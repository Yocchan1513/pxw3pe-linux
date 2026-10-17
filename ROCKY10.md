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
| 実機確認 | 2026-10-09。DKMS でのビルド、地デジと BS の受信、B-CAS でのデコード、4チューナー同時録画を確認済み |

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

このリポジトリのディレクトリで実行します。

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

## 6. recisdb を入れる(選局・録画)

選局と録画には [`recisdb`](https://github.com/kazuki0824/recisdb-rs) を使います。
Rocky 用のパッケージは配布されていないので、ソースからビルドします。ビルドの際は、次の2点に注意してください。

- **1.3.1 以降を使う。** 1.2.x は DVB で使うと、録画の終了時にフリーズすることがあります。
  feed を止めた後も、受信用スレッドが `read` で待ち続けてしまうためです。この問題は 1.3 で直っています。
- **`--features dvb` を付けてビルドする。** 付けないと、DVB のデバイスを chardev として扱ってしまい、
  `Cannot tune the device. (Unexpected Linux error: 1)` になります。

DVB 機能を使うには `libdvbv5` が必要です。Rocky 10 では、本体は CRB リポジトリにありますが、`-devel` パッケージがありません。
そこで、同じ版の v4l-utils のソースからヘッダーだけを取り出して使います。

```sh
sudo dnf config-manager --set-enabled crb
sudo dnf install -y rust cargo clang cmake gcc-c++ pcsc-lite pcsc-lite-devel pcsc-lite-ccid libdvbv5 v4l-utils

# libdvbv5 のヘッダーと pkg-config ファイルをホームディレクトリに用意する
V=$(rpm -q --qf '%{VERSION}' libdvbv5)        # 例: 1.26.1
P=$HOME/.local/libdvbv5
mkdir -p $P/include $P/lib/pkgconfig && cd /tmp
curl -sSfLO https://linuxtv.org/downloads/v4l-utils/v4l-utils-$V.tar.xz
tar -xJf v4l-utils-$V.tar.xz --wildcards "*/lib/include/libdvbv5/*"
cp -r v4l-utils-$V/lib/include/libdvbv5 $P/include/
ln -sf /usr/lib64/libdvbv5.so.0 $P/lib/libdvbv5.so
cat > $P/lib/pkgconfig/libdvbv5.pc <<EOF
prefix=$P
Name: libdvbv5
Description: libdvbv5 (headers from v4l-utils $V)
Version: $V
Libs: -L\${prefix}/lib -ldvbv5
Cflags: -I\${prefix}/include
EOF

PKG_CONFIG_PATH=$P/lib/pkgconfig cargo install --locked --features dvb \
    --git https://github.com/kazuki0824/recisdb-rs --tag 1.3.1 recisdb
```

カードリーダーを使えるようにするには、`sudo systemctl enable --now pcscd.socket` を実行します。

### B-CAS カードと polkit

Rocky の pcscd は、polkit でアクセス権を確認しています。既定では、画面の前でログインしているユーザーしかカードを使えません。
SSH 経由や systemd から一般ユーザーとして録画すると、カードにアクセスできず(`Access denied`)、
デコードに失敗します。その場合は、次のようなルールを追加してください(`azuki15` の部分は、実際に使うユーザー名に置き換えます)。

```sh
sudo tee /etc/polkit-1/rules.d/90-pcscd-recisdb.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if ((action.id == "org.debian.pcsc-lite.access_pcsc" ||
         action.id == "org.debian.pcsc-lite.access_card") &&
        subject.user == "azuki15") {
        return polkit.Result.YES;
    }
});
EOF
```

### 受信の確認

```sh
R=~/.cargo/bin/recisdb
$R checksignal -d /dev/dvb/adapter2/frontend0 -c T26                 # 地デジ: S/N が表示される
$R tune -i /dev/dvb/adapter2/frontend0 -c T26 -t 10 t26.ts
$R tune -i /dev/dvb/adapter0/frontend0 -c BS15_0 --tsid 16625 -t 10 bs15.ts
```

BS/CS を DVB で受信するときは、`--tsid` で TSID を指定してください。
`FE_SET_VOLTAGE: Unknown error 524` と表示されますが、これはこのボードが LNB 給電に対応していないことによるもので、問題ありません。

2026-10-09 に実機で確認した結果は次のとおりです。

| チャンネル | ビットレート | TSID | デコード |
|-----------|-------------|------|---------|
| 地デジ T26 | 約 16Mbps | 32032 | OK |
| BS15_0 | 約 20Mbps | 16625 | OK |
| BS1_0 | 約 16Mbps | 16400 | OK |

4つのチューナーで同時に30秒録画しても、ドロップや TEI エラーは出ず、デコードも正常でした。

アダプタ番号の対応は次のとおりです。

| アダプタ | 用途 |
|---------|------|
| `adapter0`, `adapter1` | BS/CS |
| `adapter2`, `adapter3` | 地デジ |

## 7. Mirakurun で配信する(TVTest などから視聴)

Mirakurun 4.x は Node.js(22 または 24)で直接動かし、systemd のサービスにします。
公式では PM2 を使う方法が「Legacy」とされているので、代わりに [`contrib/mirakurun/mirakurun.service`](contrib/mirakurun/mirakurun.service) を使います。
recisdb が選局と B-CAS のデコードまで行うので、Mirakurun 側の `decoder` は設定しません。

```sh
sudo dnf install -y nodejs npm
sudo install -m 0755 ~/.cargo/bin/recisdb /usr/local/bin/recisdb   # root で動くサービスから使うので、ホームの外に置く
sudo npm install -g mirakurun@4.1.5 --foreground-scripts

sudo mkdir -p /usr/local/etc/mirakurun
sudo install -m 0644 contrib/mirakurun/tuners.yml /usr/local/etc/mirakurun/tuners.yml
sudo install -m 0644 contrib/mirakurun/mirakurun.service /etc/systemd/system/mirakurun.service
```

### channels.yml を作る

- **BS/CS:** DVB では TSID を指定する必要があります。受信した NIT から一覧を作ります。
  ```sh
  recisdb tune -i /dev/dvb/adapter0/frontend0 -c BS15_0 --tsid 16625 -t 15 /tmp/bs.ts
  recisdb tune -i /dev/dvb/adapter1/frontend0 -c CS2 -t 15 /tmp/cs.ts
  python3 contrib/mirakurun/nit2channels.py /tmp/bs.ts /tmp/cs.ts | sudo tee /usr/local/etc/mirakurun/channels.yml
  ```
- **地デジ:** Mirakurun のスキャン機能で追加します。tuners.yml で、GR のチャンネル番号の先頭に `T` を付けて recisdb に渡すようにしてあります。

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now mirakurun
sudo firewall-cmd --permanent --add-port=40772/tcp && sudo firewall-cmd --reload
curl -s -N -X PUT "http://127.0.0.1:40772/api/config/channels/scan?type=GR&minCh=13&maxCh=62&refresh=false"
sudo systemctl restart mirakurun      # スキャン後に再起動が必要です
curl -s http://127.0.0.1:40772/api/services | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
```

2026-10-09 の実機確認では、GR 8ch、BS 26TS、CS 12TS で、合計 120 サービスが登録されました。
他の PC から地デジと BS の映像を取得でき、暗号化されたまま残ったパケットはありませんでした。

信号の弱い TS は、サービススキャンで `stream has closed before get services` になります
(今回は CS12、CS16、CS18。周波数の高い CS ほど弱くなる傾向がありました)。
受信できない TS は、`channels.yml` で `isDisabled: true` にしてください。

### Windows の TVTest から見る

[BonDriver_Mirakurun](https://github.com/Chinachu/BonDriver_Mirakurun/releases) v2.0(x64)の DLL と ini を、TVTest の BonDriver フォルダに置きます。
ini は次のように設定します。

```ini
[GLOBAL]
SERVER_HOST="192.168.1.249"
SERVER_PORT=40772
DECODE_B25=0      ; デコードはサーバー側の recisdb で行うので 0 にする
PRIORITY=0
SERVICE_SPLIT=0
```

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
