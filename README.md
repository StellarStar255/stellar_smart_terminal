# Smart Terminal

## 使用方法
```shell
python app.py
```

## Install

``` shell
pip install PyQt6 pyte


## 启动智能终端（默认运行claude）
python smart_terminal.py

## 运行其他命令
python smart_terminal.py -c bash

## 查看历史会话
python smart_terminal.py --history

## 列出所有会话
python smart_terminal.py --list

## 导出指定会话
python smart_terminal.py --export 20260113_191500 --format html

```


## Mac Desktop Python Package Install
```shell

# 解决代理证书问题
/usr/local/bin/pip3 install -r requirements.txt --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org

export http_proxy=http://127.0.0.1:1081/
export https_proxy=http://127.0.0.1:1081/
/usr/local/bin/pip3 install -r requirements.txt


```