# Chat Relay

Chat Relay syncs your selected local chats and Goals from **Codex** and **Claude Code for VS Code** between Windows devices. It runs in the notification area and uses a shared folder, such as a private folder synced by MEGA.

## Install

1. Download and extract the ZIP from [Releases](https://github.com/cesarreynab/chat-relay/releases).
2. Open PowerShell in the extracted folder. You need Python 3.11 or later with Tcl/Tk and Python on your PATH.
3. Run the installer.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1
```

The installer creates a local virtual environment, adds a Start menu shortcut, and opens **Settings**. Chat Relay also starts when you sign in. If the icon is absent from the notification area, check the hidden icons. To update, quit Chat Relay from the tray and run the new installer.

## Set up two devices

1. Right-click the Chat Relay tray icon and open **Settings** on both devices. For **Shared folder**, choose each device's local copy of the same private synced folder. Use a dedicated folder outside either assistant's profile.
2. For **Local project root**, choose the folder that contains your projects on each device. The drive can differ. A chat from `C:\Projects\demo` can open under `D:\Projects\demo` on the second device. The project folder must exist on both.
3. Select the chats on the source device and click **Save selection**. The initial selection is empty.
4. On the second device, select each chat when it appears as **Shared** and save. Close VS Code and the corresponding assistant so Chat Relay can apply the import. When the chat shows **Imported**, reopen the assistant.

After you accept a chat, future revisions arrive automatically once the assistant closes. Chat Relay checks the shared folder every 12 seconds and backs up changes to previously published chats when the assistant closes. Transfer time depends on your file sync client.

## What syncs

| Assistant | Scope |
| --- | --- |
| Local Codex | Chat history and its native Goal, including the objective, status, budget, and counters. This release supports initialized profiles using `state_5.sqlite`, `goals_1.sqlite`, and `thread_history_1.sqlite`. Complete a local chat to initialize a fresh profile, and use the same Codex version on both devices. Only chats with a local transcript appear in Settings. |
| Local Claude Code | The main transcript and native `/goal` events. We verified resuming an active Goal with the executable bundled with VS Code extension version 2.1.329. Claude restores the condition and resets its elapsed time, turn count, and token-spend baseline on resume. [Claude Code Goals](https://code.claude.com/docs/en/goal#resume-with-an-active-goal) |

Project files, cloud-only sessions, checkpoints, auxiliary files, and external attachments are outside the scope of this release. Use the same account for each assistant on both devices.

Revisions live in `ChatRelay-v1` inside the shared folder. The ZIPs contain native chat data, so keep the folder private and accessible only to trusted devices. SHA-256 checksums detect incomplete transfers. If both devices edit a chat from the same base revision, Chat Relay reports a **Conflict** and keeps both copies. Deselecting a chat stops local sync. Published copies remain in the shared folder.

## Development

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m chat_relay --settings
```
