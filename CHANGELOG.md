# Changelog

## [0.1.1](https://github.com/joolertan/whitebird-api/releases/tag/v0.1.1) (2026-11-08)

Every package had its own changelog and tag up to this version (`python-v0.1.1`, `node-v0.1.1`,
`go/v0.1.1`, `rust-v0.1.1`); the project is released as one since.


### Features

* generate account, KYC and notification resources ([24c57c0](https://github.com/joolertan/whitebird-api/commit/24c57c058342df5e2146fcbd42949606af78292d))
* generate exchange, balance and merchant resources ([b63943c](https://github.com/joolertan/whitebird-api/commit/b63943c55c9875660b9f9e85f83fcf7712d52553))
* generate ICO resources ([690d30a](https://github.com/joolertan/whitebird-api/commit/690d30a8f1b409714a74309b1b1fa461ddf256db))
* generate market data and trading resources ([641bc73](https://github.com/joolertan/whitebird-api/commit/641bc731f29e10593d1fbf4dc812832bc63af540))
* generate payment provider resources ([babefc3](https://github.com/joolertan/whitebird-api/commit/babefc38d112af3dd4d658ef2ef44b09744c3a38))
* **go:** add client core with auth, retries, iterators and SSE ([73c52ea](https://github.com/joolertan/whitebird-api/commit/73c52ea80b9889968d8e1f10ee9b00de4f30f615))
* **go:** captcha solver, operation MFA and examples ([6699fb8](https://github.com/joolertan/whitebird-api/commit/6699fb8bd6176ae460a70f6efe14a8493019a772))
* **go:** generate brokerage resources ([449756f](https://github.com/joolertan/whitebird-api/commit/449756f26ebeef42964c08fbc5fbc34521c84ba4))
* **node:** add client core with auth, retries, pagination and SSE ([98c1818](https://github.com/joolertan/whitebird-api/commit/98c18186dc4fd85dd263ee112df2dfb04d17c2d4))
* **node:** captcha solver, operation MFA and examples ([1ea1329](https://github.com/joolertan/whitebird-api/commit/1ea1329ff8e5a63ba258a787435a8886a9db8a23))
* **node:** generate brokerage resources ([6b1317c](https://github.com/joolertan/whitebird-api/commit/6b1317c869bba680b854595d57d54824897c0b8a))
* **python:** accept coroutine code providers in AsyncWhitebird ([b761b4a](https://github.com/joolertan/whitebird-api/commit/b761b4a9dff0b4417ce60bb097014c247502ed36))
* **python:** add sync and async clients with auth, retries, pagination and SSE ([832e23e](https://github.com/joolertan/whitebird-api/commit/832e23ec957cb365efabf4c695f0720f106ee241))
* **python:** captcha solver, operation MFA and examples ([fbc9b83](https://github.com/joolertan/whitebird-api/commit/fbc9b835c71da1754721a8934c1183f5c57aff79))
* **python:** generate brokerage resources ([175ec45](https://github.com/joolertan/whitebird-api/commit/175ec458e2fc3c7839dce050b8e932c8778bcfd2))
* **rust:** accept async and fallible two-factor code providers ([f4e4845](https://github.com/joolertan/whitebird-api/commit/f4e484541749bc9e81021d88b58ca4ec246ae51e))
* **rust:** add Rust SDK core ([6e4e257](https://github.com/joolertan/whitebird-api/commit/6e4e25738581b520205f11e4b48e7bdbf76e2103))
* **rust:** captcha solver, operation MFA and examples ([8a06345](https://github.com/joolertan/whitebird-api/commit/8a06345a34168e7eab23df34b71450477a18143e))
* **rust:** generate resources for every service ([1ffd842](https://github.com/joolertan/whitebird-api/commit/1ffd84244de0d02a7dafc32f755b4dcb2bf5347b))


### Bug Fixes

* check two-factor status before every password login ([b180779](https://github.com/joolertan/whitebird-api/commit/b1807797a0652b961e8a1ffb4033d3087f64fead))
* **go:** sign in like the web app and harden retries and streams ([cd100cb](https://github.com/joolertan/whitebird-api/commit/cd100cb420280cdf5cf2c27c9ac69b82819c2ba5))
* **node:** sign in like the web app and keep decimals exact ([fab2728](https://github.com/joolertan/whitebird-api/commit/fab27285865b4fbb51ff948a827d0237908bd8fc))
* **python:** accept union response types in the engine ([50cbba7](https://github.com/joolertan/whitebird-api/commit/50cbba728aa589d16a370cee24aae83752b6c60c))
* **python:** sign in, refresh and retry like the web app ([5b1e4de](https://github.com/joolertan/whitebird-api/commit/5b1e4de419ec075f3e78d367c6249b9f81308233))
* **python:** use Decimal for brokerage amounts ([9e750d5](https://github.com/joolertan/whitebird-api/commit/9e750d5dcc377f04c8cdeba20928931dadfb30c4))
* **rust:** sign in like the web app and fix the blocking client ([375b88b](https://github.com/joolertan/whitebird-api/commit/375b88b9b12f5b94416b43d46124ed0456d525b0))


### Documentation

* add READMEs, contributing guide and security policy ([1d42f36](https://github.com/joolertan/whitebird-api/commit/1d42f366470d26b7986b04a6a504ee0e26704722))
* describe what the SDKs do instead of how the spec is built ([5260784](https://github.com/joolertan/whitebird-api/commit/526078462e652d3d15e90d5ac48dfcf6d3285807))
* **spec:** describe ICO operation types in English ([ce94835](https://github.com/joolertan/whitebird-api/commit/ce94835377e65b2c3e51ad04eaf9091f33ca33bb))
