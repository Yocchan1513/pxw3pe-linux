# tools/tsanalysis — 取り込んだ TS の解析ツール

Python 3.12 の標準ライブラリだけで動く。ハードウェア、`/dev`、sudo には触れない。
`python3 -I` で実行してよい(同じフォルダのモジュールは、スクリプトが自分で sys.path に足す)。
放送の MULTI2(B-CAS)スクランブルは**解除しない**。TSC != 0 のパケットは数えるだけ。

| ファイル | 内容 |
|---|---|
| `tscheck.py` | TS の健全性チェックと、暗号化状態の判定 |
| `cardscramble.py` | カード側(暗号チップ)のスクランブル解除の仮説 (a)(b) を PAT で検証する |
| `selftest.py` | 合成 TS と DES の既知解ベクタによる自己テスト(unittest) |
| `tslib.py` | 共通部品(CRC32/MPEG-2、パケットの読み取りと再同期、PSI、方式 (a)(b)) |
| `des.py` | 純 Python の DES-ECB(方式 (b) の検証用。遅い) |

## tscheck.py

```
python3 -I tools/tsanalysis/tscheck.py cap.ts [--tag-ok] [--quiet-pids] [--max-packets N] [--tags 47,C7]
```

- 先頭バイトが 0x47 と 0xC7(ブリッジの ISDB-T タグ)のパケットを受け付け、0x47 に正規化する。ずれていれば再同期する。
- タグが残っていると WARNING を出す。タグが付いているのが分かっている場合は `--tag-ok` で警告を消す。
- 出力するもの: パケット数、PCR(最初に見つかった PCR PID)から求めた時間とビットレート、TEI の数、PID ごとのパケット数、CC エラー、TSC != 0 の割合、PAT と PMT(CRC が正しいときだけ)。
- CC の判定規則: ペイロードがあるパケットだけ CC が進む。重複は 1 回まで許す。adaptation field の discontinuity_indicator があればリセットする。PID 0x1FFF は対象外。TEI が立ったパケットは数えるだけで、CC の判定には使わない。
- 最後の VERDICT 行:
  - `payload looks clear`: PAT の CRC が正しい
  - `card-scrambled`: PID 0 の TSC は 0 なのに、PAT として正しいセクションが一つもない
  - `broadcast-scrambled`: PAT と PMT は正しいが、ES の PID の過半が TSC != 0(MULTI2)
- 終了コード: 0 = 異常なし、1 = 異常あり(再同期、TEI、CC エラー、card-scrambled など)、2 = 引数やファイルのエラー

## cardscramble.py

```
python3 -I tools/tsanalysis/cardscramble.py cap.ts [--max 64] [-q]
python3 -I tools/tsanalysis/cardscramble.py --hex "c7 60 00 14 f2 c1 ..."   # 途中までのパケットでも可。複数指定できる
python3 -I tools/tsanalysis/cardscramble.py cap.ts --keys keys.txt           # 方式 (b) も試す
python3 -I tools/tsanalysis/cardscramble.py cap.ts --write-out out.ts --scheme a   # ファイル全体に適用する
```

- 方式 raw(変換なし)、(a) ptx の XOR [ptx]、(b) w3pe の XOR + DES-ECB [w3pe] を PID 0 のパケットに適用し、PAT として正しいか判定する(pointer_field、table_id 0x00、section_syntax_indicator、section_length、CRC32)。
  - 状態の意味: `valid` = CRC まで正しい。`plausible` = 見えている範囲のヘッダは正しいが、データが途中で切れていて CRC を確認できない。`invalid` = どれかの判定に失敗。
- `--xor-seed` で方式 (a) のシード(4 バイト)を変えられる(既定は ptx の 2F 46 56 E3)。
- 鍵ファイルの書式(方式 (b)):`even=16 桁の hex`、`odd=16 桁の hex`、`xor=8 桁の hex`。`#` 以降はコメント。**鍵は手元にない。** 鍵の値は表示しない。
- 鍵がなくても分かる構造の解析:
  - 内容が同じ PAT が、CC が違ってもまったく同じ暗号文になるか(ECB 的か、パケットごとに変わるか)
  - 1 パケットのペイロードの中で、同じ 8 バイトのブロックが繰り返されるか(PAT の 0xFF 詰め物が見えるか)。周期的な XOR だと仮定して、マスクとシードを逆算する。同じブロックが 128 バイトの境界(even 鍵と odd 鍵の境目)をまたいで繰り返されるかも表示する。
- 終了コード: (a) か (b) で valid が 1 つでもあれば 0、なければ 1

## selftest.py

```
python3 -I tools/tsanalysis/selftest.py -v
```

合成 TS(PAT、PMT、PCR、映像、音声、NULL)を作り、再同期、切れたパケット、CC の規則(欠落、重複 1 回、重複 2 回、discontinuity、AF のみのパケット、NULL)、TEI、放送スクランブル、方式 (a)、架空の鍵での方式 (b) を検出できることを確かめる。DES の既知解ベクタ 5 本(Grabbe の例、FIPS 81、NIST SP 800-17)と、CRC32/MPEG-2 の検査値(`123456789` → 0x0376E6E7)も確かめる。
環境変数 `TSANALYSIS_TMP` に書き込めるディレクトリを指定すると、鍵ファイルを読むテストも走る(指定しなければ skip)。

2026-10-14 の結果: 20 テストすべて OK(ソース確認ではなく、合成データでの確認)。

## 実データについて(2026-10-14 時点)

- 実機で取り込んだ PAT の先頭 32 バイト(`c7 60 00 14 f2 c1 76 4f ...`)は、raw でも方式 (a) でも pointer_field が範囲外になり、PAT にならない(**実機確認**)。方式 (b) は鍵がないので確認できない(**未確認**)。
- 次の一手: PID 0 のパケットを 188 バイト丸ごと複数取って、`cardscramble.py` に渡す。CC が違うのに暗号文が同じなら ECB 的。ペイロード内でブロックが繰り返されていれば、詰め物が見えている。

## 性能

純 Python なので、`tscheck.py` は約 5 万パケット/秒(J2900 で 16.5 Mbps の TS の約 4 倍速)。方式 (b) の `--write-out` は DES が遅いので、長いファイルには向かない。

## dvbtest.py(DVB デバイスからの取り込み)

recisdb と同じ手順(FE_SET_PROPERTY で ISDB-T を選局 → FE_READ_STATUS でロックを待つ →
demux に PID 0x2000 / DMX_OUT_TS_TAP → dvr0 を読む)で、指定した秒数の TS を保存する。
B-CAS の復号はしないので、ドライバ単体の確認に使う。

```
python3 -I tools/dvbtest.py --adapter 2 --channel 27 --seconds 30 out.ts
python3 -I tools/tsanalysis/tscheck.py out.ts
```

ioctl の番号と構造体の大きさは、x86-64 の `/usr/include/linux/dvb` から C で出力した値。
