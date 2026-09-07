# Changelog

## [0.0.2](https://github.com/kalynnka/octomate/compare/octomate-cli-v0.0.1...octomate-cli-v0.0.2) (2026-09-06)


### Features

* add independent package releases and manual deployment ([9e4a7d7](https://github.com/kalynnka/octomate/commit/9e4a7d757b661310445441cabf770f1719de26fc))
* commit turns at Stop and drain the remote tail per turn ([557b9d0](https://github.com/kalynnka/octomate/commit/557b9d01b207d47d12db7c24a73a4d628027b0bf))
* DeepSeek Harness agent tentacle with native-session ingest ([25e1970](https://github.com/kalynnka/octomate/commit/25e19703cc68db962d3423af6b8fcdf835fde6b3))
* every configured credential names a person ([77df91c](https://github.com/kalynnka/octomate/commit/77df91ce83762e5f386b66b8312295dbd8ffaebb))
* give every thread its own workspace ([f4a99f2](https://github.com/kalynnka/octomate/commit/f4a99f2eb6915e126e4706f2ccaf01acb9a186c3))
* give the client its own config file and decouple it from the server ([4ad740c](https://github.com/kalynnka/octomate/commit/4ad740cdc6c797deca940f0e0b62bd599dff839d))
* ingest claude through the stream only ([ea6928f](https://github.com/kalynnka/octomate/commit/ea6928fb03d5c2d02f58f0485ca21ccc5a539b9e))
* ingest codex through the stream only and settle turns at Stop ([3ee47eb](https://github.com/kalynnka/octomate/commit/3ee47eb1fe470cbd699a87ce42c563ec4eea5a51))
* ingest native dsh sessions through hooks and a gateway-reading tail ([f467621](https://github.com/kalynnka/octomate/commit/f467621254db127c57bafb21d9ad2687236fcfdd))
* install the gateway MCP config with one verb-first command ([7490012](https://github.com/kalynnka/octomate/commit/74900124ef9414739deb0d25417511dc6edebdc2))
* manage plist server startup and upgrades from CLI ([e846b2f](https://github.com/kalynnka/octomate/commit/e846b2f742fcea7afd42a99ec22f800558090526))
* one credential, minted on the client and registered by hand ([6c39305](https://github.com/kalynnka/octomate/commit/6c39305625b91e24e00e8ca84f553af709d624d6))
* one MCP server named octomate for every agent ([b3c9e75](https://github.com/kalynnka/octomate/commit/b3c9e75aa3fb80a583ea1b35bcf657b225a07775))
* prepare package releases and manual deployment ([29e6522](https://github.com/kalynnka/octomate/commit/29e6522719a17adc4eb4b957efd0eb700d2afa27))
* promote the hook secret to the deployment's one secret ([873380e](https://github.com/kalynnka/octomate/commit/873380e85e6fd57589297dba39ac38465c0ec7b6))
* release packages independently ([8b4e7d8](https://github.com/kalynnka/octomate/commit/8b4e7d8d7c4feab606a320a5f9e221858f95ea4f))
* resolve the hook target from OCTOMATE_URL at fire time ([4b2cb0a](https://github.com/kalynnka/octomate/commit/4b2cb0ac2baf56b1392aeefca369b552757f3a83))
* scope the claude mcp config to local, user, or project ([da1f5dc](https://github.com/kalynnka/octomate/commit/da1f5dc1433a5da0df69943259fcf73b5d948618))
* serve the MCP servers from the Octomate app behind the secret ([a77b9dc](https://github.com/kalynnka/octomate/commit/a77b9dc2b41783a985d1d1562c9ccdbd1e67f2cb))
* stream remote Codex rollouts over the transcript stream ([900c769](https://github.com/kalynnka/octomate/commit/900c7695795ff46ff850dc88b431d430abf63c41))
* stream remote native-session transcripts over the hook pipe ([0d199b7](https://github.com/kalynnka/octomate/commit/0d199b7e318817e5fb5b9443884ec78ce81f6fa3))
* stream-only native session ingest (OCTO-41) ([661a0db](https://github.com/kalynnka/octomate/commit/661a0db010950cc510a6c415ca16b088ff29c211))
* wire driven Codex turns to the served gateway MCP endpoint ([b640975](https://github.com/kalynnka/octomate/commit/b640975b7fd241a60bfd64e90775b111f2428588))


### Bug Fixes

* a driven codex turn speaks as its kicker or not at all ([e0c0700](https://github.com/kalynnka/octomate/commit/e0c0700a01b40c63f8f44735f3f225747bcf068f))
