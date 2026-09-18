# Sourced (not executed) by run.sh and smoke/*.sh.
# Ensures uv + an isolated Python 3.12 venv + deps are present, without
# touching system Python or requiring Homebrew.

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python package/version manager) to your user site..."
  pip3 install --user -q --disable-pip-version-check uv
fi
export PATH="$HOME/Library/Python/3.9/bin:$HOME/.local/bin:$PATH"

if [ ! -d ".venv" ]; then
  echo "Fetching Python 3.12 and creating virtual environment..."
  uv python install 3.12
  uv venv --python 3.12 .venv
fi

uv pip install --python .venv/bin/python -q -r requirements.txt
