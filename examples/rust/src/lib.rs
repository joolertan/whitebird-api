//! Shared setup of the examples: credentials from the environment and a session kept in a file.

use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::PathBuf;
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use whitebird::{BoxError, Captcha, CaptchaSolver, Client, CodeProvider, TokenStore, Tokens, Totp};

/// Error type of the examples' `main` functions.
pub type Error = Box<dyn std::error::Error>;

/// Keeps the session tokens in a JSON file so that later runs skip the login. The file grants
/// access to the account, so it is written readable by its owner only.
pub struct FileTokenStore {
    path: PathBuf,
}

impl FileTokenStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    /// Writes `json` to a new file next to the session file, then renames it over the session
    /// file, so that a crash or a concurrent run never leaves a half-written session.
    fn write(&self, json: &[u8]) -> io::Result<()> {
        static WRITES: AtomicU64 = AtomicU64::new(0);
        let mut name = self.path.file_name().unwrap_or_default().to_owned();
        name.push(format!(
            ".{}-{}.tmp",
            process::id(),
            WRITES.fetch_add(1, Ordering::Relaxed)
        ));
        let temporary = self.path.with_file_name(name);
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        let result = options.open(&temporary).and_then(|mut file| {
            file.write_all(json)?;
            file.sync_all()?;
            fs::rename(&temporary, &self.path)
        });
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}

impl TokenStore for FileTokenStore {
    fn load(&self) -> Option<Tokens> {
        // A missing or damaged file means no session: the client signs in again.
        let json = fs::read(&self.path).ok()?;
        serde_json::from_slice(&json).ok()
    }

    fn save(&self, tokens: &Tokens) {
        let saved = serde_json::to_vec_pretty(tokens)
            .map_err(io::Error::other)
            .and_then(|json| self.write(&json));
        if let Err(error) = saved {
            eprintln!(
                "cannot save the session to {}: {error}",
                self.path.display()
            );
        }
    }

    fn clear(&self) {
        match fs::remove_file(&self.path) {
            Err(error) if error.kind() != io::ErrorKind::NotFound => eprintln!(
                "cannot delete the session in {}: {error}",
                self.path.display()
            ),
            _ => {}
        }
    }
}

/// Reads a required environment variable.
///
/// # Errors
///
/// Returns an error naming the variable if it is not set.
pub fn var(name: &str) -> Result<String, Error> {
    env::var(name).map_err(|_| format!("set the {name} environment variable").into())
}

/// A client signed in as `WHITEBIRD_USERNAME` / `WHITEBIRD_PASSWORD` that keeps its session in
/// `whitebird-session.json`. Two-factor codes are asked for in the terminal, or computed from
/// `WHITEBIRD_TOTP_SECRET` when it is set; a sign-in captcha, when Whitebird asks for one, is
/// solved on a page opened in a browser.
///
/// # Errors
///
/// Returns an error if a variable is missing or the TOTP secret is not valid base32.
pub fn client() -> Result<Client, Error> {
    let codes = match env::var("WHITEBIRD_TOTP_SECRET") {
        Ok(secret) => Totp::new(&secret)?.into(),
        Err(_) => CodeProvider::from_fn(ask_code),
    };
    Ok(Client::builder()
        .credentials(var("WHITEBIRD_USERNAME")?, var("WHITEBIRD_PASSWORD")?)
        .otp(codes)
        .captcha(CaptchaSolver::from_fn(|captcha| ask_captcha(&captcha)))
        .token_store(FileTokenStore::new("whitebird-session.json"))
        .build()?)
}

/// Reads a two-factor code from the terminal.
fn ask_code() -> Result<String, BoxError> {
    print!("Two-factor code: ");
    io::stdout().flush()?;
    let mut code = String::new();
    io::stdin().read_line(&mut code)?;
    Ok(code.trim().to_owned())
}

/// Writes the captcha to a page with a slider and reads the position a person found there.
fn ask_captcha(captcha: &Captcha) -> Result<u32, BoxError> {
    let page = CAPTCHA_PAGE
        .replace("{background}", &STANDARD.encode(&captcha.background_image))
        .replace("{piece}", &STANDARD.encode(&captcha.puzzle_piece))
        .replace("{y}", &captcha.position_y.to_string());
    let path = env::temp_dir().join("whitebird-captcha.html");
    fs::write(&path, page)?;
    print!(
        "Open {} in a browser, slide the piece into the gap and enter the number shown: ",
        path.display()
    );
    io::stdout().flush()?;
    let mut answer = String::new();
    io::stdin().read_line(&mut answer)?;
    Ok(answer.trim().parse()?)
}

const CAPTCHA_PAGE: &str = r#"<!doctype html>
<meta charset="utf-8">
<title>Whitebird captcha</title>
<div style="position: relative; width: 290px; height: 192px">
  <img src="data:image/png;base64,{background}">
  <img id="piece" src="data:image/png;base64,{piece}" style="position: absolute; left: 0; top: {y}px">
</div>
<input id="slider" type="range" min="0" max="250" value="0" style="width: 290px">
<p>Slider position: <b id="value">0</b></p>
<script>
  slider.oninput = () => {
    piece.style.left = slider.value + "px";
    value.textContent = slider.value;
  };
</script>
"#;
