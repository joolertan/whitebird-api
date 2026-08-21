import { n as e } from "./runtime.js";
var keys;
var store;
var getEnv;
var setEnv;
function init() {
  return (init = e(() => {
    keys = {
      VITE_AUTH_URL: `VITE_AUTH_URL`,
      VITE_AUTH_TOKEN_URL: `VITE_AUTH_TOKEN_URL`,
      VITE_TRADING_BASE_URL_TRADING_SPOT_LIVE: `VITE_TRADING_BASE_URL_TRADING_SPOT_LIVE`
    };
    store = {};
    getEnv = e => store[e] ?? ``;
    setEnv = e => Object.assign(store, e);
  }))();
}
init();
setEnv({
  BASE_URL: `/`,
  VITE_AUTH_URL: `https://auth-server.whitebird.io/api/v1`,
  VITE_AUTH_TOKEN_URL: `https://auth-server.whitebird.io`,
  VITE_AUTH_BASIC_TOKEN: `Y2xpZW50OnNlY3JldA==`,
  VITE_TRADING_BASE_URL_TRADING_SPOT_LIVE: `https://trading-service.whitebird.io`,
  VITE_TRADING_BASE_URL_TRADING_SPOT_DEMO: `https://trading-demo-service.whitebird.io`,
  VITE_STRAPI_URL: `https://content.whitebird.io`,
  VITE_GA_KEY: `G-123`
});
export { getEnv as G, keys as K, init as i };
