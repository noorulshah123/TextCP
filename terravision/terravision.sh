# clone and install python deps (into the same python env your kernel uses)
cd $HOME
git clone https://github.com/patrickchugh/terravision.git
cd terravision
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# make the CLI script executable
chmod +x terravision

# Option A: add the repo dir to PATH
echo 'export PATH="$HOME/terravision:$PATH"' >> ~/.bashrc
# Option B (recommended): symlink just the script into ~/.local/bin
mkdir -p ~/.local/bin
ln -sf "$HOME/terravision/terravision" ~/.local/bin/terravision
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

# reload shell env for the terminal session
source ~/.bashrc

# verify
which terravision
terravision --help
