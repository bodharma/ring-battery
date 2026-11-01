# Find a backup for Ring Battery

Ring Battery needs a saved ring key or an **existing unencrypted Finder backup of your iPhone**. The backup must contain Oura’s app data from after that ring was paired with the phone.

The app never creates a backup or starts a phone transfer. If you want to avoid new transfers, use the existing-backup steps below and skip the optional creation section.

## Find an existing backup without connecting your phone

1. Open **Finder**.
2. Press **Shift–Command–G** to open the folder-location dialog.
3. Paste this path and press Return:

   ```text
   ~/Library/Application Support/MobileSync/Backup/
   ```

4. Each device-backup folder inside has a long identifier as its name. In Ring Battery, select your ring and use **Connect using existing backup** to search this location automatically.
5. For manual selection, click **Choose an existing backup…** and select the device-backup folder containing `Manifest.db`, rather than its parent `Backup` folder. You do not need to open or edit the database files.

If the folder is missing or empty, this Mac has no backup in the standard location. A backup may be stored on another drive or Mac; an existing folder can be selected manually. An iCloud backup or an Oura website CSV/JSON export cannot be imported by this app.

Apple documents the [local backup location and backup management](https://support.apple.com/en-gb/108809).

## Identify the right backup

If your iPhone is **already visible in Finder**, select it, open **General → Manage Backups**, then Control-click the appropriate backup and choose **Show in Finder**. This identifies the folder by device and date instead of guessing from its long name. You do not need to connect the phone just to use the folder-location steps above.

A lock icon in the backup list means the backup is encrypted. Ring Battery currently cannot decrypt it, even if you know the backup password. Keep its encryption enabled; use an already extracted ring key/database instead. See Apple’s [backup instructions](https://support.apple.com/en-gb/108796) for the encryption controls.

If several distinct ring keys are found, automatic setup stops and asks you to choose one existing backup. A backup made before the ring was paired may not contain its key.

## If you have no usable backup

With **no new backup transfers**, the remaining supported source is a previously saved authentication key or an exported Oura app database containing that key. Use **Advanced key import** in the connection window.

If you have neither source, this release cannot connect to an already paired ring. Selecting the ring over Bluetooth alone does not recover its key. Do not delete or reset the ring to work around this limitation.

## Optional: create a local backup yourself

**This starts a new phone-data transfer. Skip it if you want existing data only.** These are instructions for readers who deliberately choose to make a backup; Ring Battery does not run them.

1. Connect the iPhone to the Mac with a cable, unlock it, and approve the computer trust prompts if shown.
2. In Finder, select the iPhone under **Locations** and open **General**.
3. Select **Back up all of the data on your iPhone to this Mac**. Review encryption before proceeding: this release cannot import encrypted backups. Do not disable existing encryption for Ring Battery; use another supported key source instead.
4. Choose **Back Up Now** and wait for Finder to report completion. This copies phone data onto your Mac, including data beyond Oura; it is a device backup rather than an Oura-only export.
5. Return to Ring Battery and use **Connect using existing backup**, or locate the completed folder with the instructions above.

Apple’s [Mac backup guide](https://support.apple.com/en-gb/108796) covers the current Finder steps. Do not choose a restore or erase action; neither is required for Ring Battery.

## Common setup messages

| Message | Next action |
| --- | --- |
| No existing local backups | Check the folder above or select a backup stored elsewhere. Without a backup or saved key, setup cannot finish under the no-transfer restriction. |
| Backups are encrypted | Keep encryption enabled. This release needs an already extracted key/database instead. |
| Backups could not be read | Try **Choose an existing backup…**. macOS may restrict access; do not upload the backup to get help. |
| Multiple keys, invalid key data, or too many backups | Choose one known device-backup folder. If its key data is invalid or still ambiguous, use the correct saved key under Advanced. |
| Ring authentication failed | The selected ring and recovered key may not match. Select the correct ring and backup. A factory reset is not needed. |

Keys, backups and health records remain private. Do not attach them to GitHub issues.
