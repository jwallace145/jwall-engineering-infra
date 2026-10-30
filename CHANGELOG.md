# Changelog

## [0.13.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.12.0...v0.13.0) (2026-10-30)


### Features

* run consumer tasks on dedicated, ephemeral EC2 instances ([#72](https://github.com/jwallace145/jwall-engineering-infra/issues/72)) ([3c28a7f](https://github.com/jwallace145/jwall-engineering-infra/commit/3c28a7ffed617c019ee269705245b7becd9bccaf))

## [0.12.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.11.1...v0.12.0) (2026-10-29)


### Features

* **bot:** pin the bot image by name and bump the library to 0.11.0 ([#69](https://github.com/jwallace145/jwall-engineering-infra/issues/69)) ([a21047f](https://github.com/jwallace145/jwall-engineering-infra/commit/a21047fc8786c79e3dc1b68d67e1d22706d42786))


### Bug Fixes

* **bot:** wait for a release's assets before building its image ([#71](https://github.com/jwallace145/jwall-engineering-infra/issues/71)) ([f545357](https://github.com/jwallace145/jwall-engineering-infra/commit/f545357f8428aae9bb5087a31c825da330448d21))

## [0.11.1](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.11.0...v0.11.1) (2026-10-29)


### Bug Fixes

* **bot:** resolve project paths from the project file, not the working directory ([#64](https://github.com/jwallace145/jwall-engineering-infra/issues/64)) ([cdf1e5d](https://github.com/jwallace145/jwall-engineering-infra/commit/cdf1e5d1ecbd4f38044f806d121504f3f478b368))

## [0.11.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.10.1...v0.11.0) (2026-10-28)


### Features

* add the container-image and lichess-bot kinds ([#60](https://github.com/jwallace145/jwall-engineering-infra/issues/60)) ([7c0271a](https://github.com/jwallace145/jwall-engineering-infra/commit/7c0271a3231f8e3a1f08c6896b58a13bbe4f3a12))
* **bot:** derive bot role ARNs from config.yaml instead of repository variables ([#63](https://github.com/jwallace145/jwall-engineering-infra/issues/63)) ([0a111e4](https://github.com/jwallace145/jwall-engineering-infra/commit/0a111e4be2117b74903076ad9bcdbfebb4e4cabd))

## [0.10.1](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.10.0...v0.10.1) (2026-10-22)


### Documentation

* say which commit types release and how to ship hidden-type dependency bumps ([#58](https://github.com/jwallace145/jwall-engineering-infra/issues/58)) ([91b598a](https://github.com/jwallace145/jwall-engineering-infra/commit/91b598a87aeadb941f71bbfa8df563d2100c6da0))

## [0.10.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.9.1...v0.10.0) (2026-10-21)


### Features

* **report:** brand the plan and preview comments and post them as the sites app ([#49](https://github.com/jwallace145/jwall-engineering-infra/issues/49)) ([d7b8f13](https://github.com/jwallace145/jwall-engineering-infra/commit/d7b8f133d8c12c9b8021ed3ec524ab27d1a65a0a))

## [0.9.1](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.9.0...v0.9.1) (2026-10-19)


### Bug Fixes

* **dns:** bump the library to 0.9.1 so the verification TXT value is not double-quoted ([#47](https://github.com/jwallace145/jwall-engineering-infra/issues/47)) ([5030e27](https://github.com/jwallace145/jwall-engineering-infra/commit/5030e273c1042aecc0370b81ee204f3d78c40efe))

## [0.9.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.8.1...v0.9.0) (2026-10-19)


### Features

* **dns:** manage the GitHub Pages domain-verification TXT record and bump the library to 0.9.0 ([#45](https://github.com/jwallace145/jwall-engineering-infra/issues/45)) ([ce8d5eb](https://github.com/jwallace145/jwall-engineering-infra/commit/ce8d5ebf8a92ad3ad8668761efdf226b1371719b))

## [0.8.1](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.8.0...v0.8.1) (2026-10-17)


### Documentation

* describe the sites CLI, its release pipeline and the version pins ([#43](https://github.com/jwallace145/jwall-engineering-infra/issues/43)) ([4efa8be](https://github.com/jwallace145/jwall-engineering-infra/commit/4efa8be16a3d4211dc451eaf0f4096ee09e0a2f6))

## [0.8.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.7.0...v0.8.0) (2026-10-17)


### Features

* **cli:** serve HTML with must-revalidate and keep cached assets for an hour ([#41](https://github.com/jwallace145/jwall-engineering-infra/issues/41)) ([8f0fab9](https://github.com/jwallace145/jwall-engineering-infra/commit/8f0fab9c0c4d881a49fb41580b22e8290c0f8042))

## [0.7.0](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.6.1...v0.7.0) (2026-10-17)


### Features

* **actions:** add the composite action and reusable site workflows ([#38](https://github.com/jwallace145/jwall-engineering-infra/issues/38)) ([54a3437](https://github.com/jwallace145/jwall-engineering-infra/commit/54a343763957de1b4885b571b40e7aecfd8cdd94))


### Bug Fixes

* **actions:** use the action from the reusable workflow commit instead of a bumped tag ([#39](https://github.com/jwallace145/jwall-engineering-infra/issues/39)) ([1c46986](https://github.com/jwallace145/jwall-engineering-infra/commit/1c46986869ae2dcad933516c5ff0fd569cf87c7f))

## [0.6.1](https://github.com/jwallace145/jwall-engineering-infra/compare/v0.6.0...v0.6.1) (2026-10-17)


### Bug Fixes

* **ci:** publish the CLI image from the prod environment and allow re-publishing a tag ([#36](https://github.com/jwallace145/jwall-engineering-infra/issues/36)) ([bfb3a10](https://github.com/jwallace145/jwall-engineering-infra/commit/bfb3a10e6357d914f7a66d6189480734d9ca7674))

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
