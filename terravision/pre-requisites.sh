# in a SageMaker terminal for your Jupyter environment
# Graphviz (binaries) — needed for diagram rendering
sudo apt-get update && sudo apt-get install -y graphviz

# Git (likely already present)
sudo apt-get install -y git

# Terraform (CLI) — choose a Linux x86_64 version that matches your image
TF_VERSION=1.9.8
curl -LO https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_amd64.zip
unzip terraform_${TF_VERSION}_linux_amd64.zip
mkdir -p $HOME/bin
mv terraform $HOME/bin/
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
terraform version
