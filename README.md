# pxw3pe-linux (azuki15 改良版)

> このリポジトリは、inaba-garnet 氏の [inaba-garnet/pxw3pe-linux](https://github.com/inaba-garnet/pxw3pe-linux)
> をフォークした改良版です。元のコミット履歴はそのまま残しています。
> ドライバ本体の作者は inaba-garnet 氏です。不具合は、まずこのリポジトリで報告してください。
>
> 元のリポジトリからの主な変更点:
>
> - 作者の `fix/kernel7-pci-irq-intx` ブランチを取り込み(`PCI_IRQ_INTX` への対応と DKMS のビルド修正)
> - PX-W3PE Rev1.2 への対応。元リポジトリの [Issue #1](https://github.com/inaba-garnet/pxw3pe-linux/issues/1)
>   で Choro5083 氏が公開したパッチを適用しました。レジスタ配置を自動で判別し、ISDB-S の PLL 書き込みで
>   バイトが欠ける問題も修正しています。
> - `dkms.conf` の `AUTO_INSTALL` を `AUTOINSTALL` に修正(カーネル更新時に自動で再ビルドされるようにしました)
> - Rocky Linux 10(RHEL 10 系)への対応。標準カーネルに DVB が入っていないため、`dvb-core` を同梱して
>   DKMS で一緒にビルドします。手順は [`ROCKY10.md`](ROCKY10.md) を参照してください。
>   Rocky Linux 10.2 の実機で、地デジ・BS の受信と、4チューナーの同時録画を確認しています。
> - recisdb は 1.3.1 以降を、`--features dvb` を付けてビルドしてください。1.2.x は録画の終了時にフリーズすることがあります。

PLEXのPX-W3PE Rev1.3というPCIeテレビチューナーカード用のLinux DVBドライバです。
ブリッジにASICENのASV5220、制御チップにASIE5606を使っていて、ISDB-SとISDB-Tの
復調コアを2基ずつ積んでいます。

カードからはDVBアダプタが4つ見えます。

| アダプタ | チューナー | 復調コア |
|---------|-------|------------|
| `adapter0` | ISDB-S (S0) | 0x32 |
| `adapter1` | ISDB-S (S1) | 0x36 |
| `adapter2` | ISDB-T (T0) | 0x30 |
| `adapter3` | ISDB-T (T1) | 0x34 |

## 動作環境

- PLEX PX-W3PE Rev1.3
- x86-64のLinux、kernel 5.15系で動作確認
- 録画・選局はrecisdb 1.2系

Rocky Linux 10(RHEL 10系)は標準カーネルにDVBが入っていません。同梱の`dvb-core/`を使う手順を
[`ROCKY10.md`](ROCKY10.md)にまとめています。

---

## ファームウェアの準備

ASIE5606は、トランスポートストリームがホストに届く前にDESでスクランブルをかけてきます。
だから受信したストリームは、ドライバ側で解いてやらないと中身が読めません。

解くのに使うのはASV5606のチップシードと2本のDES鍵、それにXORシード。ドライバは
probe時に`request_firmware()`で`/lib/firmware/pxw3pe.fw`から読み込みます。

この鍵はカードのモデル固有のプロプライエタリな値なので、本リポジトリには含めていません。
`pxw3pe.fw`は公式ドライバ`asv5220_dtv.ko`から自分でビルドしてください。手順は
[`firmware/README.md`](firmware/README.md)にあります。

PLEX公式サイトでの配布はすでに終わっていますが、Wayback Machineから入手できます。
**使う`asv5220_dtv.ko`はビルドによって結果が変わる**ので、次の表で確認してください。

| 入手元(Wayback) | `asv5220_dtv.ko` のビルド | 結果 |
|---|---|---|
| [`PX-W3PE_LinuxDriver_ver.1.0.0.zip`](https://web.archive.org/web/20150505112623/http://plex-net.co.jp/plex/px-w3pe/PX-W3PE_LinuxDriver_ver.1.0.0.zip) の `Driver/64bit.tar.gz` | 2013-04-23、vermagic `2.6.32-279.5.1.el6`、sha256 `402c8fd855d1d1102397974baa2c945e7aba7de88c4b2283fafb1fa103b20d99` | **正しい鍵が得られる**(Rev1.3 で、復号した PAT の CRC が一致することを確認) |
| [`px-w3pe/64bit.tar.gz`](https://web.archive.org/web/20130325235643/http://plex-net.co.jp/plex/px-w3pe/64bit.tar.gz) | 2013-01-31、vermagic `2.6.32-279.el6`、sha256 `e8772c6293085034520c4bdc355d1cfe34b47e12138e825ccb5bc5817f338a0a` | **使わないこと**。抽出は成功してしまうが、シードは同じなのに DES 鍵2本と XOR シードが違う値になり、復号できない |

zip の sha256 は `75e22c30421a39622562b207c15ddea47ebbe55babeb83379c7409d700b6e977` です。
`PX-SERIES_ver.1.0_Linux_Driver.zip` は USB チューナー(S3U、S3U2、W3U3)用のドライバで、
ASV5220 用の`.ko`は入っていません。

firmwareを入れずにロードしてもエラーにはなりません。ただしカード独自のスクランブルが
かかったままのストリームしか出てこないので、まともには使えません。これは放送波のARIB
MULTI2/CASとは別の層で、firmwareが無いとそこに届かず、B25デコードも実行できません。

---

## ビルドとインストール

ビルドには稼働中カーネルのヘッダと`gcc`、`make`が要ります。

```sh
make                      # pxw3pe.ko をビルド
sudo make install         # インストールして depmod まで実行
```

firmware blobをビルドするなら、手元の`asv5220_dtv.ko`のほかに`gcc`、`objcopy`、
`python3`も必要です。

```sh
python3 firmware/make_pxw3pe_fw.py --from-ko /path/to/asv5220_dtv.ko pxw3pe.fw
sudo cp pxw3pe.fw /lib/firmware/
```

あとはモジュールを読み込ませます。

```sh
sudo modprobe pxw3pe      # （または: sudo insmod pxw3pe.ko）
dmesg | tail
# 期待される出力: "key firmware 'pxw3pe.fw' loaded (descramble available)"
#                 "registered 4 DVB adapters"
```

### DKMSを使う場合

カーネルを更新しても自動で再ビルドされるので、入れ直す手間が省けます。

```sh
sudo cp -r . /usr/src/pxw3pe-1.0
sudo dkms add    -m pxw3pe -v 1.0
sudo dkms install -m pxw3pe -v 1.0
```

---

## 使い方

選局と録画は[`recisdb`](https://github.com/kazuki0824/recisdb-rs)で。以下はBSの例です。

```sh
recisdb checksignal -d /dev/dvb/adapter0/frontend0 -c BS15_0
recisdb tune -i /dev/dvb/adapter0/frontend0 -c BS15_0 -t 30 out.ts
```

### モジュールパラメータ

| パラメータ | 既定値 | 意味 |
|-------|---------|---------|
| `descramble` | `1` | 鍵firmwareがあるときASV5606のトランスポートDESデスクランブルを適用する |
| `use_msi` | `1` | MSI割り込みを使う（無効なら従来のINTx） |
| `s_acquire_retries` | `1` | キャリアがロックしないときにISDB-SのPLLと捕捉をリトライする回数 |
| `pkt_num`, `n_dmabuf` | — | DMAリングのチューニング |
| `adapter_nr` | 未設定 | DVBアダプタ番号の固定。登録順はS0、S1、T0、T1。例: `adapter_nr=2,3,0,1`でISDB-Tがadapter0/1になる |

## 注意事項

LNB給電はサポートしません。このリビジョンは基板上のレギュレータがDCをまともに出せず、
F端子で測っても0Vのままです。衛星LNBは外部電源から給電してください。

---

## ライセンス

GPL-2.0です。詳しくは[`LICENSE`](LICENSE)を見てください。

firmware blobの中の4つの暗号フィールドはカードのモデルに固有の値で、あなたがライセンスを
持つ`asv5220_dtv.ko`からビルド時に取り出します。これらはここでは配布していません。

## 謝辞

- ブリングアップの裏取りには、knight-rider氏のGPLドライバ[`ptx`](https://github.com/knight-rider/ptx)（同梱のPX-Q3PE用`pxq3pe`）と、nns779氏の[`px4_drv`](https://github.com/nns779/px4_drv)を参照しました。
