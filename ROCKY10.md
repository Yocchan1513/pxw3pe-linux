# Rocky Linux 10 でのセットアップ

Rocky Linux 10(RHEL 10 系)の標準カーネルは、デジタルTV(DVB)機能を無効にしてビルドされています
(`# CONFIG_MEDIA_DIGITAL_TV_SUPPORT is not set`)。そのため、このドライバが使う `dvb-core` もありません。

そこでこのブランチでは、Rocky Linux 10.2 のカーネルソース
(`kernel-6.12.0-211.16.1.el10_2`)から取り出した `dvb-core` を [`dvb-core/`](dvb-core/) に同梱しています。
カーネルに DVB 機能がない場合だけ、Makefile と DKMS が `dvb-core.ko` も一緒にビルドします。

確認済みの環境は次のとおりです。

| 項目 | 内容 |
|------|------|
| OS | Rocky Linux 10.2 (Red Quartz) |
| カーネル | `6.12.0-211.16.1.el10_2.0.1.x86_64` |
| カード | PX-W3PE(PCI `188b:5220`、サブシステム `0b06:0001`) |
| Secure Boot | 無効(有効な場合は、モジュールへの署名と MOK 登録が別途必要です) |
| 実機確認 | 2026-10-09。DKMS でのビルド、モジュールの読み込み、DVB アダプタ4つの登録まで確認済み |

---

## 1. 必要なパッケージを入れる

```sh
sudo dnf install -y epel-release          # dkms は EPEL にあります
sudo dnf install -y dkms gcc make kernel-devel-$(uname -r)
```

`kernel-devel-$(uname -r)` が見つからないときは、動いているカーネルがリポジトリより古いのが原因です。
カーネルを更新して再起動してから、もう一度実行してください。

```sh
sudo dnf update -y kernel && sudo reboot
```

## 2. 鍵ファームウェアを置く

`pxw3pe.fw` の作り方は [`firmware/README.md`](firmware/README.md) を参照してください。
できあがったファイルを Rocky 側にコピーしてから、次を実行します。

```sh
sudo install -m 0644 pxw3pe.fw /lib/firmware/pxw3pe.fw
sudo restorecon -v /lib/firmware/pxw3pe.fw   # SELinux のラベルを付け直す
```

## 3. DKMS でビルドしてインストールする

このリポジトリ(`rocky10` ブランチ)のディレクトリで実行します。

```sh
sudo mkdir -p /usr/src/pxw3pe-1.0
git archive HEAD | sudo tar -x -C /usr/src/pxw3pe-1.0   # .git を含めずにコピーする

sudo dkms add     -m pxw3pe -v 1.0
sudo dkms build   -m pxw3pe -v 1.0
sudo dkms install -m pxw3pe -v 1.0
dkms status
# 例: pxw3pe/1.0, 6.12.0-211.16.1.el10_2.0.1.x86_64, x86_64: installed
```

`/lib/modules/$(uname -r)/extra/` に `pxw3pe.ko.xz` と `dvb-core.ko.xz` の2つが入っていれば成功です
(Rocky 10 の dkms 3.4 では、インストール先が `extra/` になります)。
`Deprecated feature: CLEAN` という警告が出ますが、無視してかまいません。

カーネルが複数入っている場合は、`-k <カーネル版>` を付けて `dkms install` を実行すると、それぞれのカーネル向けにビルドされます。

## 4. 読み込んで確認する

```sh
sudo modprobe pxw3pe          # 依存する dvb_core と libdes も自動で読み込まれます
sudo dmesg | grep -iE 'pxw3pe|dvb' | tail -n 20
```

`dmesg` に次の行が出ていれば正常です。

- `key firmware 'pxw3pe.fw' loaded (descramble available)`
- `register layout: standard ...`(Rev1.3 の基板)または `register layout: alternate ...`(Rev1.2 の基板)
- `registered 4 DVB adapters`

あわせて、次のコマンドも確認してください。

```sh
lspci -k -d 188b:5220     # "Kernel driver in use: pxw3pe" と出ること
ls /dev/dvb/              # adapter0 〜 adapter3 があること
```

PCI ID のエイリアスが登録されているので、次回からは起動時に自動で読み込まれます。

## 5. 一般ユーザーで使えるようにする

`/dev/dvb` のデバイスは `video` グループに属しています。

```sh
sudo usermod -aG video $USER    # 反映するには一度ログアウトしてください
```

選局と録画には [`recisdb`](https://github.com/kazuki0824/recisdb-rs) を使います。
使い方は [README](README.md#使い方) を参照してください。

アダプタ番号の対応は次のとおりです。

| アダプタ | 用途 |
|---------|------|
| `adapter0`, `adapter1` | BS/CS |
| `adapter2`, `adapter3` | 地デジ |

---

## カーネルを更新したとき

`dkms.conf` に `AUTOINSTALL="yes"` を設定してあるので、新しいカーネルでは自動で再ビルドされます。
ただし、新しいカーネルの `kernel-devel` を先に入れておく必要があります(`dnf update` なら通常は同時に入ります)。

同梱の `dvb-core` は `6.12.0-211` のソースです。Rocky 10 の 6.12 系の中での更新ならそのまま使える見込みです。
ビルドに失敗したり読み込めなくなったりしたときは、そのカーネルの SRPM から
`drivers/media/dvb-core/*.c` を取り出し、`dvb-core/` の中身を差し替えてください。

## トラブルシューティング

| 症状 | 確認すること |
|------|-------------|
| `ASIE auth/enable failed` | `/lib/firmware/pxw3pe.fw` が存在するか、`restorecon` を実行したかを確認してください。 |
| `no responsive I2C engine` | `sudo modprobe pxw3pe regmap=1` で、Rev1.2 用のレジスタ配置を強制して試してください。 |
| `Unknown symbol dvb_*` | `dvb-core.ko` が入っていません。`dkms status` を確認し、必要なら `sudo dkms install` をやり直してください。 |
| BS/CS が映らない | LNB 給電に対応していないので、アンテナには外部から電源を供給してください。 |

パラメータを毎回指定したい場合は、次のように設定ファイルに書いておきます。

```sh
echo 'options pxw3pe regmap=1' | sudo tee /etc/modprobe.d/pxw3pe.conf
```

## アンインストール

```sh
sudo modprobe -r pxw3pe dvb_core
sudo dkms remove -m pxw3pe -v 1.0 --all
sudo rm -rf /usr/src/pxw3pe-1.0
```
