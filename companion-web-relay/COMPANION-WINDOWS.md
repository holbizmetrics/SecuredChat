# SecuredChat companion — Windows quick start

This package is only the localhost Python companion. Continue using the Chromium extension from
the canonical Windows tree:

`D:\FromGitHubEtc\BrowserExtensions\Chrome Extensions\SecuredChat Web Bridge\extension`

## 1. Validate the paths

From PowerShell in the extracted package directory, adjust the two repository paths:

```powershell
.\start_companion.ps1 `
  -SecuredChatRoot "D:\FromGitHubEtc\SecuredChat" `
  -BusRoot "D:\FromGitHubEtc\securedchat-bus" `
  -Check
```

The successful check prints JSON containing `"status": "ready"` and version `0.1.2`.

## 2. Start the companion

```powershell
.\start_companion.ps1 `
  -SecuredChatRoot "D:\FromGitHubEtc\SecuredChat" `
  -BusRoot "D:\FromGitHubEtc\securedchat-bus"
```

The first real start prints a newly generated bridge token once and listens only on
`http://127.0.0.1:8765`. Leave this PowerShell window open.

## 3. Connect the extension

Clear any GitHub token previously entered in the extension. Paste only the token printed by the
companion, save it, and press **Test bridge**. The expected result is `ready`, version `0.1.2`, room
`prometheus-relay`.

Start with Mode 0 and DRAFT. AUTO exists in the extension's `feature/mode-2-auto` branch but is
disabled at the source (`MODE2_SUBMIT_ENABLED = false`) and cannot be enabled from any UI.

**What is new in 0.1.2:** `/v1/health` now returns an `instance` field — a per-process id
regenerated on every start. The extension binds its Mode-2 arming window to that exact value and
disarms every site the moment it changes, so a companion restart can no longer leave an armed
AUTO window alive. A companion older than 0.1.2 reports no instance id and therefore cannot be
armed against at all; that is deliberate, because an older companion is exactly the case where a
restart is invisible. The bridge
token is stored under `%USERPROFILE%\.config\securedchat-web-relay\`; it is unrelated to GitHub
credentials.

## Verification

```powershell
python -m unittest discover -s companion\tests -v
```

The package was released with 17 passing companion tests.
