mod common;

use std::sync::{Arc, Mutex};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use common::*;
use serde_json::{Value, json};
use whitebird::{Captcha, CaptchaSolver, Error, ErrorKind};
use wiremock::matchers::{header_exists, method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

const BACKGROUND: &[u8] = b"\x89PNG background";
const PIECE: &[u8] = b"\x89PNG piece";

async fn serve_captchas(server: &MockServer) {
    Mock::given(method("GET"))
        .and(path(CAPTCHA_PATH))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "backgroundImage": STANDARD.encode(BACKGROUND),
            "puzzlePiece": format!("data:image/png;base64,{}", STANDARD.encode(PIECE)),
            "positionY": 40,
        })))
        .mount(server)
        .await;
}

fn invalid_captcha() -> ResponseTemplate {
    ResponseTemplate::new(400).set_body_json(json!({
        "status": "INVALID_CAPTCHA",
        "message": "Invalid captcha",
    }))
}

/// A solver that records the captchas it saw and answers 123.
fn recording_solver() -> (CaptchaSolver, Arc<Mutex<Vec<Captcha>>>) {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&seen);
    let solver = CaptchaSolver::from_fn(move |captcha| {
        log.lock().unwrap().push(captcha);
        Ok(123)
    });
    (solver, seen)
}

fn captcha_answer(request: &Request) -> Value {
    let header = request.headers["captcha"].to_str().unwrap();
    serde_json::from_slice(&STANDARD.decode(header).unwrap()).unwrap()
}

fn sent_to<'a>(requests: &'a [Request], target: &str) -> Vec<&'a Request> {
    requests.iter().filter(|r| r.url.path() == target).collect()
}

#[tokio::test]
async fn solves_the_captcha_before_logging_in_without_two_factor() {
    let server = MockServer::start().await;
    mfa_check(false).mount(&server).await;
    serve_captchas(&server).await;
    password_grant()
        .and(header_exists("captcha"))
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    let (solver, seen) = recording_solver();

    builder(&server)
        .credentials("me@example.com", "secret")
        .captcha(solver)
        .build()
        .unwrap()
        .login()
        .await
        .unwrap();

    let requests = server.received_requests().await.unwrap();
    let seen = seen.lock().unwrap();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].background_image, BACKGROUND);
    assert_eq!(seen[0].puzzle_piece, PIECE);
    assert_eq!(seen[0].position_y, 40);
    let fetched = sent_to(&requests, CAPTCHA_PATH);
    let (_, request_id) = fetched[0]
        .url
        .query_pairs()
        .find(|(key, _)| key == "requestId")
        .unwrap();
    assert_eq!(request_id, seen[0].request_id);
    assert_eq!(request_id.len(), 36);
    let token = sent_to(&requests, TOKEN_PATH);
    // The web app sends the value of its range input: a string.
    assert_eq!(
        captcha_answer(token[0]),
        json!({ "sliderPosition": "123", "requestId": request_id })
    );
    assert!(!form(token[0]).iter().any(|(name, _)| name == "code"));
}

#[tokio::test]
async fn retries_rejected_answers_with_fresh_captchas() {
    let server = MockServer::start().await;
    mfa_check(false).mount(&server).await;
    serve_captchas(&server).await;
    password_grant()
        .respond_with(invalid_captcha())
        .up_to_n_times(2)
        .with_priority(1)
        .mount(&server)
        .await;
    password_grant()
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .mount(&server)
        .await;
    let (solver, seen) = recording_solver();

    builder(&server)
        .credentials("me@example.com", "secret")
        .captcha(solver)
        .build()
        .unwrap()
        .login()
        .await
        .unwrap();

    let seen = seen.lock().unwrap();
    assert_eq!(seen.len(), 3);
    assert_ne!(seen[0].request_id, seen[1].request_id);
}

#[tokio::test]
async fn gives_up_after_three_rejected_answers() {
    let server = MockServer::start().await;
    mfa_check(false).mount(&server).await;
    serve_captchas(&server).await;
    password_grant()
        .respond_with(invalid_captcha())
        .expect(3)
        .mount(&server)
        .await;
    let (solver, _) = recording_solver();

    let error = builder(&server)
        .credentials("me@example.com", "secret")
        .captcha(solver)
        .build()
        .unwrap()
        .login()
        .await
        .unwrap_err();

    assert!(error.is_captcha_required(), "{error:?}");
    assert_eq!(error.api().unwrap().error_code(), Some("INVALID_CAPTCHA"));
}

#[tokio::test]
async fn reports_captcha_required_without_a_solver() {
    let server = MockServer::start().await;
    mfa_check(false).expect(1).mount(&server).await;
    serve_captchas(&server).await;
    password_grant()
        .respond_with(grant("unused", "unused"))
        .expect(0)
        .mount(&server)
        .await;

    let error = builder(&server)
        .credentials("me@example.com", "secret")
        .build()
        .unwrap()
        .login()
        .await
        .unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::CaptchaRequired));
    assert!(error.is_authentication());
    let requests = server.received_requests().await.unwrap();
    assert!(sent_to(&requests, CAPTCHA_PATH).is_empty());
}

#[tokio::test]
async fn a_captcha_demand_to_a_two_factor_account_is_reported() {
    let server = MockServer::start().await;
    mfa_check(true).mount(&server).await;
    serve_captchas(&server).await;
    password_grant()
        .respond_with(ResponseTemplate::new(400).set_body_json(json!({
            "error": "CAPTCHA_REQUIRED",
            "error_description": "Captcha is required",
        })))
        .expect(1)
        .mount(&server)
        .await;
    let (solver, seen) = recording_solver();

    let error = builder(&server)
        .credentials("me@example.com", "secret")
        .captcha(solver)
        .build()
        .unwrap()
        .login()
        .await
        .unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::CaptchaRequired));
    assert!(error.is_authentication());
    assert!(seen.lock().unwrap().is_empty());
    let requests = server.received_requests().await.unwrap();
    assert!(sent_to(&requests, CAPTCHA_PATH).is_empty());
}

#[tokio::test]
async fn asks_for_the_code_when_the_captcha_login_wants_one() {
    let server = MockServer::start().await;
    mfa_check(false).mount(&server).await;
    serve_captchas(&server).await;
    code_requested().expect(1).mount(&server).await;
    code_grant("123456")
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    let (solver, seen) = recording_solver();

    builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .captcha(solver)
        .build()
        .unwrap()
        .login()
        .await
        .unwrap();

    assert_eq!(seen.lock().unwrap().len(), 1);
    let requests = server.received_requests().await.unwrap();
    let logins = sent_to(&requests, TOKEN_PATH);
    assert!(logins[0].headers.contains_key("captcha"));
    assert!(!logins[1].headers.contains_key("captcha"));
}

#[tokio::test]
async fn sends_the_code_instead_when_two_factor_is_on() {
    let server = MockServer::start().await;
    serve_captchas(&server).await;
    two_factor_login(&server, "123456", &jwt("me", 3600)).await;
    let (solver, seen) = recording_solver();

    builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .captcha(solver)
        .build()
        .unwrap()
        .login()
        .await
        .unwrap();

    assert!(seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn accepts_async_solvers() {
    let server = MockServer::start().await;
    mfa_check(false).mount(&server).await;
    serve_captchas(&server).await;
    password_grant()
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .mount(&server)
        .await;

    builder(&server)
        .credentials("me@example.com", "secret")
        .captcha(CaptchaSolver::new(|captcha| async move {
            Ok(captcha.position_y + 37)
        }))
        .build()
        .unwrap()
        .login()
        .await
        .unwrap();

    let requests = server.received_requests().await.unwrap();
    let answer = captcha_answer(sent_to(&requests, TOKEN_PATH)[0]);
    assert_eq!(answer["sliderPosition"], "77");
}

#[tokio::test]
async fn reports_solver_failures() {
    let server = MockServer::start().await;
    mfa_check(false).mount(&server).await;
    serve_captchas(&server).await;
    password_grant()
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(0)
        .mount(&server)
        .await;

    let error = builder(&server)
        .credentials("me@example.com", "secret")
        .captcha(CaptchaSolver::new(|_| async {
            Err("no one is around".into())
        }))
        .build()
        .unwrap()
        .login()
        .await
        .unwrap_err();

    assert!(matches!(error, Error::Captcha(_)), "{error:?}");
    assert_eq!(
        error.to_string(),
        "the captcha solver failed: no one is around"
    );
}
