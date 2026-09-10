# FundOS — interactive login profile for the `fundos` service account.
#
# Install:
#   # give the account an interactive shell (it ships as /sbin/nologin):
#   sudo usermod --shell /bin/bash fundos
#   sudo install -o fundos -g fundos -m 644 \
#        deploy/fundos.bash_profile /d01/fundos/.bash_profile
#
# Then `sudo -u fundos -i` (or `su - fundos`) lands you with the virtualenv
# active, in the app directory, with the environment loaded.

# --- activate the app virtualenv ---
if [ -f /d01/fundos/app/venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source /d01/fundos/app/venv/bin/activate
    cd /d01/fundos/app/current 2>/dev/null || true
fi

# --- load the FundOS environment (secrets: file is 640 root:fundos) ---
# Comment this block out if you prefer not to load secrets into an
# interactive shell.
if [ -r /etc/fundos/fundos.env ]; then
    set -a
    # shellcheck disable=SC1091
    source /etc/fundos/fundos.env
    set +a
fi
