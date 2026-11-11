#!/bin/sh
# Builds Streamlit's Google sign-in config from environment variables, then starts the app.
if [ -n "$GOOGLE_CLIENT_ID" ]; then
  mkdir -p "$HOME/.streamlit"
  cat > "$HOME/.streamlit/secrets.toml" <<TOML
[auth]
redirect_uri = "${PUBLIC_URL}/oauth2callback"
cookie_secret = "${AUTH_COOKIE_SECRET}"
client_id = "${GOOGLE_CLIENT_ID}"
client_secret = "${GOOGLE_CLIENT_SECRET}"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
TOML
fi
exec streamlit run app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true \
  --browser.gatherUsageStats=false --server.enableXsrfProtection=true --server.maxUploadSize=8
