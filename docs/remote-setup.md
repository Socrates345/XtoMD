/# Setup: the brief from a VPS and from the phone

Step-by-step instructions. What this installs and how it works is in **[remote.md](remote.md)**; its [Troubleshooting](remote.md#troubleshooting) table covers the error messages you may meet here.

Do the parts in order. Each ends with a **Check**: don't go on until it passes. Parts 1 to 3 take about an hour; part 4 is optional.

| Part | Where | What you get |
| --- | --- | --- |
| [0. Laptop](#0-on-the-laptop-first) | Laptop | The code pushed, ready to be cloned |
| [1. Venice](#1-venice) | Browser | An API key for the hosted model |
| [2. VPS](#2-the-vps) | The server | A brief every day at 17:07 |
| [3. Phone](#3-the-phone) | Termux on the Pixel | Buttons to fetch a brief, or ask for one |
| [4. Phone alone](#4-the-phone-alone-optional) | Termux on the Pixel | A brief with no VPS at all |

Placeholders used below: `VPS_ADDRESS` (the server's IP or name), `REPO_URL` (this repository's clone URL), `YOUR_VENICE_KEY`.

**You need:**

- a Venice account (venice.ai) with a few dollars of API credit;
- a VPS: any KVM server with Debian 12+ (Debian fits better here: Leaner, quieter...) or Ubuntu 24.04). 1 vCPU / 1 GB RAM / 10 GB disk is enough for this app; take 2 vCPU / 4 GB / 40 GB on a resizable plan if agents will share it later;
- the laptop, with its working `sources.yaml` and `sources/`;
- the Android phone.

## 0. On the laptop first

**0.1 Check the laptop still works.** The update tightened the JSON schemas every model is given, LM Studio included:

```powershell
python scripts\run_headless.py --smoke
```

**Check:** three `OK` lines, as before the update.

**0.2 Commit and push the update.** The VPS and the phone get the code with `git clone`, so it has to be on your remote. The work is on the branch `remote`; until you merge it into `main`, the other machines clone that branch.

```powershell
git status                      # look first: only what you mean to publish
git add -A
git commit -m "brief without the laptop: VPS and phone"
git push -u origin remote
```

**Check:** the branch `remote` on your Git host shows the `deploy/` folder.

**0.3 Have an SSH key.** If `Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub` prints nothing, make one:

```powershell
ssh-keygen -t ed25519
```

The `.pub` line is what you give the VPS provider.

## 1. Venice

1. Sign in at venice.ai, open the API settings, create an API key and copy it somewhere safe. It is shown once.
2. Add a few dollars of credit. A brief costs under a cent.

**Check:** you have the key. It is tested in step 2.4.

## 2. The VPS

### 2.1 Rent it

Create the server with Debian 12+ or Ubuntu 24.04 and paste your laptop's public key (step 0.3) where the provider asks for an SSH key.

**Check**, from the laptop: `ssh root@VPS_ADDRESS` opens a shell without asking for a password.

### 2.2 Base system

On the VPS, as root:

```bash
apt update && apt -y full-upgrade
apt -y install git python3-venv tzdata ufw unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades        # answer Yes: security updates install themselves

# SSH by key only (10-: read before any file the image shipped, and the first value read wins)
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
    > /etc/ssh/sshd_config.d/10-keys-only.conf
systemctl restart ssh

# the firewall: SSH in, nothing else
ufw allow OpenSSH && ufw --force enable

# the app's own user: no password, no sudo; your laptop's key may log in as it
adduser --disabled-password --gecos "" xmd
install -d -m 700 -o xmd -g xmd /home/xmd/.ssh
install -m 600 -o xmd -g xmd /root/.ssh/authorized_keys /home/xmd/.ssh/authorized_keys
```

Keep this root session open until the check passes.

Some providers give you a user such as `debian` or `ubuntu` instead of root. Then log in as that user, run `sudo -i` before the commands above, and in the last line copy that user's key file (`/home/ubuntu/.ssh/authorized_keys`) instead of root's.

**Check**, from a second terminal on the laptop: your admin login (`ssh root@VPS_ADDRESS`) and `ssh xmd@VPS_ADDRESS` both still work.

### 2.3 The app

From the laptop, `ssh xmd@VPS_ADDRESS`, then:

```bash
git clone -b remote REPO_URL XtoMD        # drop "-b remote" once the branch is merged into main
cd XtoMD
python3 -m venv .venv && .venv/bin/pip install -e .
```

If the repository is private, give the server a read-only key first:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_deploy -N ""
cat ~/.ssh/id_deploy.pub        # GitHub: the repository > Settings > Deploy keys > Add (leave write access off)
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_deploy -o IdentitiesOnly=yes' git clone -b remote git@github.com:OWNER/XtoMD.git XtoMD
cd XtoMD && git config core.sshCommand 'ssh -i ~/.ssh/id_deploy -o IdentitiesOnly=yes'   # so "git pull" works later
```

Then copy your configuration over. It is git-ignored, so the clone does not have it. On the laptop, in PowerShell, from the repo root:

```powershell
scp sources.yaml xmd@VPS_ADDRESS:XtoMD/
scp -r sources xmd@VPS_ADDRESS:XtoMD/
scp xmd.db xmd@VPS_ADDRESS:XtoMD/         # optional: brings 14 days of history, so "Louder than usual" works from day one
```

Leave `storage:` and `digest_dir:` in `sources.yaml` at their defaults: the daily run may write inside the repo only.

**Check**, on the VPS in `~/XtoMD`: `.venv/bin/xmd sources | head -3` lists handles of yours.

### 2.4 Settings and the model

On the VPS, in `~/XtoMD`:

```bash
cp deploy/env.example .env && chmod 600 .env
nano .env
```

Put your key after `XMD_LLM_API_KEY=` and leave the other lines as they are. If the X backend's key is in `sources.yaml`, there is nothing else to fill in. To keep less on the rented disk, also uncomment `XMD_KEEP_DAYS=14`.

Test the model (synthetic posts only, a few seconds):

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/smoke_engine.py
```

**Check:** the first line names `https://api.venice.ai/api/v1` and `qwen3-5-9b`, and lines 1, 2 and 3 all say `OK`. If line 2 or 3 says `FAIL`, stop here: see [Limits](remote.md#limits).

### 2.5 A first brief

```bash
bash deploy/brief.sh 24h
```

It prints the five steps of `make_brief.py` and takes several minutes.

**Check:** the last line starts with `written to digests/` and names a `24h-brief` file.

### 2.6 The daily timer

On the VPS, as root:

```bash
cp /home/xmd/XtoMD/deploy/vps/xmd-brief.service /home/xmd/XtoMD/deploy/vps/xmd-brief.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xmd-brief.timer
```

The timer makes a `24h` brief every day at 17:07 Europe/Paris, whatever the server's own clock is set to. To change the hour, edit `OnCalendar=` in `/etc/systemd/system/xmd-brief.timer`, then `systemctl daemon-reload`.

Now run it once the way the timer will, to prove it works inside its sandbox:

```bash
systemctl start xmd-brief.service         # returns when the brief is done, a few minutes
journalctl -u xmd-brief.service -e
```

**Check:** `systemctl list-timers xmd-brief.timer` shows the next run, and the journal ends with a `written to digests/...` line. (Made in the same hour as the one from step 2.5, this second brief is named `...-brief-2.md`: a brief never replaces another.)

## 3. The phone

### 3.1 The apps

1. Install **F-Droid** (f-droid.org), then from it **Termux** and **Termux:Widget**. Both must come from F-Droid: the Play Store build of Termux is no longer updated and cannot be mixed with F-Droid add-ons.
2. Settings > Apps > Termux > Battery: **Unrestricted**.
3. Settings > Apps > Termux > **Display over other apps**: allow. The buttons need it to open a terminal.
4. Android 14 and later: Settings > System > Developer options > **Disable child process restrictions**.

### 3.2 The code

In Termux:

```bash
pkg upgrade -y
pkg install -y git
git clone -b remote REPO_URL XtoMD        # a private repository: a deploy key of its own, as in step 2.3
cd XtoMD
bash deploy/termux/setup.sh
```

`setup.sh` installs Python and SSH, asks Android for access to your files (allow it), makes the virtual environment, creates `.env` from the template and creates the five buttons. It is safe to run again.

**Check:** it ends with `done.` and lists five names starting with `brief-`.

### 3.3 The key

In Termux:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_xmd -N ""
cat >> ~/.ssh/config <<'EOF'
Host xmd-vps
    HostName VPS_ADDRESS
    User xmd
    IdentityFile ~/.ssh/id_xmd
    IdentitiesOnly yes
EOF
cat ~/.ssh/id_xmd.pub
```

Replace `VPS_ADDRESS` in `~/.ssh/config` (`nano ~/.ssh/config`). The key has no passphrase, so that a button can use it; the next step is what limits it.

Get the line `cat` printed over to the laptop: it is a public key, so sending it to yourself is fine. Then, from the laptop, `ssh xmd@VPS_ADDRESS` and add it to the server **with the prefix that restricts it**, all on one line:

```bash
echo 'command="bash /home/xmd/XtoMD/deploy/vps/ssh-entry.sh",restrict PASTE_THE_PHONE_PUBLIC_KEY_LINE_HERE' >> ~/.ssh/authorized_keys
```

**Check**, in Termux (answer `yes` to the host-key question the first time):

```bash
ssh xmd-vps pull | tar -tf -        # lists the brief made in part 2
ssh xmd-vps ls                      # must be refused: "allowed: run 24h|since [MINUTES] | pull [DAYS]"
```

### 3.4 The buttons

Long-press the home screen > Widgets > **Termux:Widget**, and drop the list on the home screen.

| Button | Does |
| --- | --- |
| `brief-vps-latest` | Downloads the briefs the VPS made in the last day: the daily one |
| `brief-vps-24h` | Asks the VPS for a brief of the last 24 hours now, then downloads it |
| `brief-vps-10min` | The same, fitted to a 10-minute read |
| `brief-phone-24h`, `brief-phone-10min` | Make the brief on the phone itself: part 4 |

Briefs land in `Documents/xmd-briefs`. To send them elsewhere, set `XMD_BRIEF_DEST` in `~/XtoMD/.env`, for instance to a folder inside your vault if the vault is in the phone's shared storage (nothing then checks the file on its way in).

**Check:** tap `brief-vps-latest`. A terminal opens, lists the brief, and the Files app shows it in `Documents/xmd-briefs`.

## 4. The phone alone (optional)

For a brief with no VPS involved. Only the model call leaves the phone.

**4.1 Settings.** In Termux, `nano ~/XtoMD/.env` and put your Venice key after `XMD_LLM_API_KEY=`. If the X backend's key is not in `sources.yaml`, uncomment its line and fill it in too.

**4.2 Your configuration.** Bring `sources.yaml` and the `sources` folder from the laptop into the phone's `Documents` (USB, or any file transfer), then in Termux:

```bash
cp ~/storage/shared/Documents/sources.yaml ~/XtoMD/
cp -r ~/storage/shared/Documents/sources ~/XtoMD/
rm -r ~/storage/shared/Documents/sources.yaml ~/storage/shared/Documents/sources     # other apps can read Documents
```

**4.3 Test the model:**

```bash
cd ~/XtoMD
set -a; . ./.env; set +a
.venv/bin/python scripts/smoke_engine.py
```

**Check:** three `OK` lines, then tap `brief-phone-24h`: it ends with `written to ...` and `copied ... to .../xmd-briefs`.

The phone has its own database, so prefer the `24h` buttons here: `since` would mean "since the phone's last digest", not the VPS's.

**A daily run on the phone** is possible but not dependable: Android decides when background work runs and may delay or skip it. If you want it anyway, install the **Termux:API** app from F-Droid, then:

```bash
pkg install -y termux-api
termux-job-scheduler --job-id 1 --script ~/.shortcuts/brief-phone-24h --period-ms 86400000 --network any --persisted true
```

`termux-job-scheduler --help` lists the options; `termux-job-scheduler --cancel --job-id 1` removes it.

## Done: what a day looks like

- At 17:07 the VPS makes the brief by itself.
- When you want to read: tap `brief-vps-latest`, then open the file from `Documents/xmd-briefs` (or move it into the vault).
- When you want one now, or shorter: `brief-vps-24h` or `brief-vps-10min`.
- If the VPS is down: `brief-phone-24h`, once part 4 is done.
- At home, the laptop works as before. Its briefs and the VPS's are independent.

Keeping it running (changing the hour, updating the code, revoking the phone) is in [remote.md](remote.md#running-it-day-to-day).
