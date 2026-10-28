# Running as a Lichess bot

This repository runs permanently as a Lichess bot on infrastructure managed by
[jwall-engineering-infra](https://github.com/jwallace145/jwall-engineering-infra).
It declares itself in `jwall-engineering-sites.yaml` and keeps no Terraform
and no AWS credentials.

- **Releasing ships the bot.** Publishing a `vX.Y.Z` release runs
  `.github/workflows/release-to-lichess.yml`, which builds an image from the
  release's Linux asset, pushes it and rolls the bot to it after the game in
  progress ends.
- **A red check on a release** means it was not deployed. The bot keeps
  playing the previous version, and the check's log says why.
- **`lichess/config.yml`** is the bot's configuration. The token stays
  `LICHESS_TOKEN_PLACEHOLDER`; the real one is injected at start.
- **Everything else is in the
  [bot projects guide](https://github.com/jwallace145/jwall-engineering-infra/blob/main/docs/lichess-bot.md):**
  rollouts, game records in S3, secrets, status, logs and the runbook.
