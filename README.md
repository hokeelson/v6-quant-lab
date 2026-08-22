# V6 Web Quant Lab

Private cloud-ready version of V6 Quant Lab.

The repository will run the live Streamlit dashboard and the local simulation worker in one service, with persistent experiment databases stored outside the code deployment.

- Trading-order API: disabled
- Market data: Alpaca + Binance
- Virtual broker: V6 local simulation engine
- Short / medium / long research sleeves
- Persistent SQLite data on a cloud volume

See `CLOUD_DEPLOY.md` for deployment setup.
