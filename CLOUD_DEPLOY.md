# V6 Web Quant Lab — cloud deployment

The repository is prepared for a one-service deployment where the Streamlit dashboard and background simulation worker share one persistent volume.

## One-time source import

Upload the latest `V6_Quant_Lab_stage7_auto_live_dashboard.zip` to the repository root with GitHub **Add file → Upload files**. The `Ingest V6 bundle` GitHub Action will automatically unpack it, apply the cloud migration, run tests, remove the ZIP, and commit the resulting source files.

## Railway setup

1. Create a Railway project from `hokeelson/v6-quant-lab`.
2. Add a persistent volume mounted at `/data`.
3. Add these variables:
   - `V6_DATA_DIR=/data`
   - `ALPACA_API_KEY=...`
   - `ALPACA_API_SECRET=...`
   - `ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets`
   - `ALPACA_DATA_BASE_URL=https://data.alpaca.markets`
   - `V6_ALLOW_PAPER_ORDERS=false`
   - `V6_PASSWORD=choose-a-private-dashboard-password`
4. Generate a public domain under Railway Networking.

## Persistence

These databases live on `/data` and survive code redeploys:

- `forward_validation.sqlite3`
- `simulation_lab.sqlite3`
- `market_cache.sqlite3`

GitHub never receives `.env`, API secrets, or SQLite experiment databases.

## Future updates

After the initial import, source files are normal GitHub files. Future V6 changes can be committed directly to this repository, and the cloud service can redeploy from the same URL without downloading ZIP updates to your PC.
