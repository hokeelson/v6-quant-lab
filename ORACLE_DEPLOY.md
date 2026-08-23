# V6 Quant Lab — Oracle Always Free Deployment

This deployment keeps Railway online until Oracle is fully verified.

## 1. Create the Oracle VM

- Create an OCI Free Tier account.
- Choose the home region carefully: Always Free compute must be created in the tenancy home region.
- Create an **Always Free eligible** compute instance.
- Recommended starting point for V6: **VM.Standard.A1.Flex, 2 OCPU, 12 GB RAM** if your Oracle Console marks that allocation Always Free eligible.
- Image: Ubuntu 22.04 or 24.04.
- Assign a public IPv4 address.
- Save the SSH private key.

Do not exceed the Always Free limits shown in your Oracle Console.

## 2. Open HTTP ingress

In OCI Networking / VCN / Security List (or Network Security Group), add an ingress rule:

- Source: `0.0.0.0/0`
- Protocol: TCP
- Destination port: `80`

The V6 Dashboard still requires `V6_DASHBOARD_PASSWORD`.

## 3. Clone the private GitHub repository

SSH into the VM, then authenticate to GitHub. One option is GitHub CLI:

```bash
sudo apt-get update
sudo apt-get install -y git curl gh
gh auth login
gh repo clone hokeelson/v6-quant-lab
cd v6-quant-lab
```

Never paste API secrets into GitHub.

## 4. Configure Oracle environment variables

```bash
cp .env.oracle.example .env.oracle
nano .env.oracle
```

Fill only on the VM:

- `ALPACA_API_KEY`
- `ALPACA_API_SECRET`
- `V6_DASHBOARD_PASSWORD`

Keep `V6_ALLOW_PAPER_ORDERS=0`.

## 5. Start V6

```bash
chmod +x oracle_install.sh
./oracle_install.sh
```

The service runs with Docker Compose and automatically restarts after VM/container restarts.
Persistent SQLite data is mounted at `/opt/v6-data`.

Open:

```text
http://<ORACLE_PUBLIC_IP>/
```

## 6. Move the Railway state

While Railway is still running, open the Streamlit page **Oracle Migration** and create/download the migration ZIP.
The ZIP uses SQLite's online backup API and does not contain environment secrets.

Copy the ZIP to the Oracle VM, then stop the Oracle container before restoring:

```bash
cd ~/v6-quant-lab
sudo docker compose -f docker-compose.oracle.yml down
sudo python3 restore_migration_backup.py /path/to/v6_oracle_migration_YYYYMMDD_HHMMSS.zip /opt/v6-data
sudo docker compose -f docker-compose.oracle.yml up -d
```

## 7. Verify before stopping Railway

Confirm on Oracle:

- 9 virtual accounts are present.
- Crypto / US stock / Taiwan stock asset counts look correct.
- Current positions match Railway.
- Model readiness is present.
- Main Worker heartbeat is fresh.
- Realtime Watchlist is non-zero.
- Crypto stream is ONLINE.
- US stock stream is ONLINE during/when market data is available.
- Broker order API remains `0`.

Only after these checks should Railway be stopped.

## Updating later

```bash
cd ~/v6-quant-lab
git pull
sudo docker compose -f docker-compose.oracle.yml up -d --build
```

The `/opt/v6-data` directory is outside the Git repository, so code updates do not overwrite the SQLite state.
