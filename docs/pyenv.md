# Using pyenv to run additional Python interpreters

## Introduction

If you have a Raspberry Pi build prior to late 2019, you may not have a version of Python interpreter install which is new enough to run TWCManager, which needs Python 3.8 or newer (3.9 for Gen3 Wall Connectors; see [Manual Installation](Software_Manual.md#default-to-python3)).

If the output of the following comamnd:

```
python3 -V
```

Is any version number prior to 3.8.0, you need a newer Python interpreter. The following commands install prerequisites for the pyenv platform, download and compile a newer Python, and then run TWCManager under that Python version. They use Python 3.7.1 as the example; substitute 3.9 or later.

Keep in mind that compiling Python can take a long time, potentially hours on a Raspberry Pi. It is expected that the following commands would take considerable time to complete.

```
apt-get install -y build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm libncurses5-dev libncursesw5-dev xz-utils tk-dev libffi-dev liblzma-dev python-openssl git

curl -L https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash

pyenv install 3.7.1

echo 'export PYENV_ROOT="${HOME}/.pyenv"' >> ~/.bashrc
echo 'if [ -d "${PYENV_ROOT}" ]; then' >> ~/.bashrc
echo '    export PATH=${PYENV_ROOT}/bin:$PATH' >> ~/.bashrc
echo '    eval "$(pyenv init -)"' >> ~/.bashrc
echo 'fi' >> ~/.bashrc

exec $SHELL -l

cd <twcmanager source directory>
python3.7 setup.py install
python3.7 -m TWCManager
```
