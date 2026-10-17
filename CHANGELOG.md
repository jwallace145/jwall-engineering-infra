# Changelog

## [0.6.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.5.0...v0.6.0) (2026-10-17)


### Features

* **cli:** add the jwall-engineering-sites package, site stack and image ([#30](https://github.com/jwallace145/jwall-engineering-infra/issues/30)) ([a53824f](https://github.com/jwallace145/jwall-engineering-infra/commit/a53824f6fc5f3638bb346971947dabd47f0c777a))


### Bug Fixes

* **cli:** read the package version from __init__.py so release PRs keep uv.lock valid ([#35](https://github.com/jwallace145/jwall-engineering-infra/issues/35)) ([05f9cf8](https://github.com/jwallace145/jwall-engineering-infra/commit/05f9cf857816a27b207234123a0de9bb32114101))

## [0.5.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.4.0...v0.5.0) (2026-10-16)


### Features

* **ecr:** add the jwall-engineering-sites repository and ECR policies ([#27](https://github.com/jwallace145/jwall-engineering-infra/issues/27)) ([de0df48](https://github.com/jwallace145/jwall-engineering-infra/commit/de0df4857e61f99e39059c1b7dfbde45b4066e45))

## [0.4.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.3.0...v0.4.0) (2026-10-16)


### Features

* **s3:** exempt the root user from the hardened bucket policies ([#24](https://github.com/jwallace145/jwall-engineering-infra/issues/24)) ([6b831b2](https://github.com/jwallace145/jwall-engineering-infra/commit/6b831b21b043e39a07e024eb1625609de508a367))

## [0.3.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.2.0...v0.3.0) (2026-10-16)


### Features

* **s3:** harden the state and site bucket policies with library 0.7.0 ([#22](https://github.com/jwallace145/jwall-engineering-infra/issues/22)) ([d7cca1c](https://github.com/jwallace145/jwall-engineering-infra/commit/d7cca1c6d7fb1201900ac8eee49a050cc53b8bf8))

## [0.2.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.1.2...v0.2.0) (2026-10-15)


### Features

* **iam:** migrate CI roles to the 0.6.0 per-role modules and managed policy catalog ([#19](https://github.com/jwallace145/jwall-engineering-infra/issues/19)) ([34d856b](https://github.com/jwallace145/jwall-engineering-infra/commit/34d856b6a25a983f9008bcca24919cadb07a7d56))

## [0.1.2](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.1.1...v0.1.2) (2026-10-15)


### Bug Fixes

* **iam:** bump jwall-engineering module to 0.5.0 ([#16](https://github.com/jwallace145/jwall-engineering-infra/issues/16)) ([f0cb9f3](https://github.com/jwallace145/jwall-engineering-infra/commit/f0cb9f3bccbd1829c843febaa3843bf36b48b441))

## [0.1.1](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.1.0...v0.1.1) (2026-10-15)


### Bug Fixes

* **tags:** set the Project tag to jwall-engineering-infra ([#14](https://github.com/jwallace145/jwall-engineering-infra/issues/14)) ([08655c2](https://github.com/jwallace145/jwall-engineering-infra/commit/08655c20481a13cb3004f369fc0884f1ef79f33d))

## 0.1.0 (2026-10-15)


### Features

* **projects:** wire site buckets and CI roles from terraform-library v0.1.0 ([282030c](https://github.com/jwallace145/jwall-engineering-infra/commit/282030ce53dbaaf598e7991652a74a4a0fda48ba))


### Bug Fixes

* **ci-roles:** trust GitHub immutable OIDC subjects for the infra roles ([f3aeb66](https://github.com/jwallace145/jwall-engineering-infra/commit/f3aeb667fbf09b15e4af7abc8f30ad9ac35466f1))
* **projects:** bump jwall-engineering/aws to 0.3.0 for immutable OIDC subjects ([#9](https://github.com/jwallace145/jwall-engineering-infra/issues/9)) ([aab434b](https://github.com/jwallace145/jwall-engineering-infra/commit/aab434bc3789c240e014fd97bcfb423bdfa42605))


### Documentation

* describe consuming the jwall-engineering modules from the terraform registry ([#8](https://github.com/jwallace145/jwall-engineering-infra/issues/8)) ([670d966](https://github.com/jwallace145/jwall-engineering-infra/commit/670d966502e08d9aa4f1b75fe26f39dbe1d2f3ab))
