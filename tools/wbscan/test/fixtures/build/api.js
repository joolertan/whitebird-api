import { n as e } from "./runtime.js";
import { G as L, K as z } from "./env.js";
var getUser;
var login;
var logoutUrl;
var logout;
var uploadDocs;
var listOrders;
var limits;
var downloadReceipt;
var unbindTable;
var unbind;
var reports;
var getReport;
var subscribe;
var connect;
var overrides;
var tradingConfig;
var sendTo;
var bindLink;
var news;
var logo;
var assetLink;
var updateAsset;
function init() {
  return (init = e(() => {
    getUser = async ({ userId: e }) => {
      let t = L(z.VITE_AUTH_URL);
      let { data: n } = await http.get(`${t}/users/${e}`, {
        params: { expand: true },
        headers: { "X-Mfa-Token": `token` }
      });
      return n;
    };
    login = async (e, t) => {
      let n = new FormData();
      n.append(`grant_type`, `password`);
      n.append(`username`, e);
      n.append(`password`, t);
      return (await http.post(`${L(z.VITE_AUTH_TOKEN_URL)}/oauth/token`, n, {
        headers: {
          Authorization: `Basic ${`Y2xpZW50OnNlY3JldA==`}`,
          "Content-Type": `application/x-www-form-urlencoded`
        }
      })).data;
    };
    logoutUrl = `https://auth-server.whitebird.io/api/v1/user/logout`;
    logout = () => http.post(logoutUrl);
    uploadDocs = e => {
      let t = new FormData();
      t.append(`file`, e);
      t.append(`comment`, `scan`);
      return { method: `post`, url: `https://kyc-service.whitebird.io/api/v1/documents`, data: t };
    };
    listOrders = (e = {}) => {
      let t = new URLSearchParams();
      if (e.status) {
        t.append(`status`, e.status);
      }
      let n = t.toString();
      return { method: `get`, url: `https://exchange-core.whitebird.io/api/v1/orders${n ? `?${n}` : ``}` };
    };
    limits = (e, t) => ({
      method: `get`,
      url: `https://exchange-core.whitebird.io/api/v1/limits?page=0&size=20`,
      params: { currency: e, ...(t && { fiatOpType: t }) }
    });
    downloadReceipt = ({ invoiceId: e, isWebView: t }) => http.get(`https://exchange-core.whitebird.io/api/v1/invoice/${e ?? ``}${t ? `/email` : ``}/receipt`, {
      responseType: `blob`
    });
    unbindTable = {
      BANK: { base: `VITE_AUTH_URL`, method: `POST`, pathPattern: e => `/card/unbind/${e}` },
      ACCOUNT: { base: `VITE_TRADING_BASE_URL_TRADING_SPOT_LIVE`, method: `DELETE`, pathPattern: e => `/accounts/${e}` }
    };
    unbind = (e, t) => {
      let n = unbindTable[t];
      return { url: `${L(z[n.base])}${n.pathPattern(e)}`, method: n.method };
    };
    reports = {
      balance: { method: `get`, getUrl: ({ baseUrl: e }) => `${e}/client/balance/report` },
      statement: { method: `post`, getUrl: ({ baseUrl: e, kind: t }) => `${e}/client/${t}/export` }
    };
    getReport = async ({ kind: e }) => {
      let t = reports[e];
      let n = t.getUrl({ baseUrl: `https://exchange-core.whitebird.io/api/v1`, kind: e });
      return t.method === `get` ? http.get(n, { responseType: `blob` }) : http.post(n, { format: `pdf` }, { responseType: `blob` });
    };
    subscribe = () => fetchEventSource(`https://notification-service.whitebird.io/api/v1/events/stream`, {
      headers: { Accept: `text/event-stream` },
      onmessage: e => e
    });
    connect = () => new EventSource(`${L(z.VITE_AUTH_URL)}/sse/connect`);
    overrides = (() => {
      let e = (e, t) => L(`VITE_TRADING_BASE_URL_${e}_SPOT_${t}`);
      return {
        BASE_URL_TRADING_SPOT_LIVE: e(`TRADING`, `LIVE`),
        BASE_URL_TRADING_SPOT_DEMO: e(`TRADING`, `DEMO`)
      };
    })();
    tradingConfig = {
      rest: {
        actions: {
          createOrder: {
            spot: {
              demo: { path: `{{BASE_URL_TRADING_SPOT_DEMO}}/api/v1/orders`, method: `POST` },
              live: { path: `{{BASE_URL_TRADING_SPOT_LIVE}}/api/v1/orders`, method: `POST` }
            }
          }
        }
      }
    };
    sendTo = (e, t) => http.post(`${L(z.VITE_AUTH_URL)}${t}`, e);
    bindLink = e => `https://mtsbank-service.whitebird.io/api/v1/card/bind?token=${e}`;
    news = () => http.get(`https://content.whitebird.io/api/news`);
    logo = { url: `https://whitebird.io/assets/logo.png` };
    assetLink = e => ({ href: `https://exchange-core.whitebird.io/api/v1/assets/${e}`, verb: `put` });
    updateAsset = async (e, t) => {
      let { href: n, verb: r } = assetLink(e);
      return http.request({ url: n, method: r, data: { name: t } });
    };
  }))();
}
export { init as i };
