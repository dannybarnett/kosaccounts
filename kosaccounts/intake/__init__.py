"""Event-driven intake (specs/README_server_intake.md).

Two long-running services replace the old hourly rclone + cron pipeline:

Stage 2  `python -m kosaccounts intake listen`      Dropbox long-poll listener (puller.listen)
         `python -m kosaccounts intake reconcile`   one full reconciliation pass (puller.reconcile_all)
Stage 3  `python -m kosaccounts intake watch`       inotify watcher on imports/<folder> (watcher.Watcher)
         `python -m kosaccounts intake auth-setup`  one-time OAuth helper, prints a refresh token

Module map (each module's docstring is its CONTRACT; implement, do not change signatures):

  types.py           RemoteFile, LongpollResult, ChangeSet, DropboxClient Protocol   (shared shapes)
  lock.py            hold_lock(path) context manager, LockHeld                        (flock)
  hashing.py         dropbox_content_hash(path) -> hex str                            (4 MiB block SHA-256)
  state.py           IntakeState(db_path): sqlite record of every imported file
  settle.py          wait_for_settle(...) -> Snapshot | None
  puller.py          plan_batch, pull_folder, reconcile_all, listen                  (Stage 2 core)
  dropbox_client.py  RealDropboxClient(app_key, app_secret, refresh_token) implementing DropboxClient
  watcher.py         Watcher(cfg, ...).run()                                          (Stage 3)

Configuration is `Config.intake` (kosaccounts/config.py: IntakeConfig). `kos_root` is the DATA
root only: kos_root/imports/<folder>, kos_root/.staging/<folder>, kos_root/state/. Secrets come
only from the environment variables DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
and must never be logged, printed (except by auth-setup, once, to the terminal) or written to disk.

Logging: stdlib `logging`, logger names "kosaccounts.intake.<module>"; systemd captures stderr
into the journal. Every completed batch is logged at INFO with folder, file count, total bytes and
the file names. Warnings for: settle timeout, name collision, verification failure, cursor reset.
"""
