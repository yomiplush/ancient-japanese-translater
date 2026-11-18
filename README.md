# ancient-japanese-translater

iPhone の IP カメラで古文（くずし字・変体仮名）を撮り、PC で OCR して
**現代口語**にライブ翻訳する GUI アプリです。

古書をめくりながら、読めない箇所をカメラでかざすと現代語訳が出ます。

```
iPhone (IPカメラ)  ──HTTP/MJPEG──▶  PC (FastAPI)
                                      │
                                      ├─ OCR: RapidOCR (PP-OCRv4, ONNX, CPU)
                                      ├─ 正規化: 変体仮名 → ひらがな / 旧字体 → 新字体
                                      └─ 翻訳: LLM (OpenAI互換 / Gemini) またはオフライン規則
                                      │
                                      ▼
                              ブラウザ GUI (ライブ表示)
```

## できること

- **縦書きの列単位で読む** — 検出した行を右から左、列内は上から下へ並べ替えます。横書きのページも自動判別します。
- **変体仮名・旧字体を正規化** — U+1B001〜U+1B11F の変体仮名を Unicode 名から機械的にひらがなへ変換（表を手書きしないので取りこぼしがありません）。旧字体→新字体も畳みます。
- **UI の写り込みを除去** — 画面撮影時の URL バー・バッテリー残量・日付などを捨て、本文だけを訳します。
- **差分だけ OCR** — 16×16 のサムネイルでフレーム間距離を測り、画面が変わっていなければ OCR をスキップ。CPU でも追随できます。
- **翻訳バックエンドは差し替え可能** — OpenAI 互換 API（OpenAI / DeepSeek / Groq / LM Studio / llama.cpp / vLLM）と Gemini。キーが無ければオフライン規則訳に自動フォールバックします。
- **iPhone と PC のペアリング** — QR コードを表示。トークン認証付き。

## 必要なもの

- Python 3.10〜3.12（3.12 推奨）。`rapidocr-onnxruntime` が 3.13 以上に未対応のため、
  3.13 では依存解決に失敗します。
- iPhone の IP カメラアプリ（下記）
- GPU 不要。12 コア CPU で 1 ページあたり約 1〜3 秒

## セットアップ

```bash
git clone https://github.com/yomiplush/ancient-japanese-translater
cd ancient-japanese-translater
uv venv --python 3.12
uv pip install -e .
cp .env.example .env      # 必要なら API キーを記入
uv run ajt doctor         # 環境チェック
uv run ajt serve
```

ブラウザで `http://localhost:8765/?t=<token>` を開きます。トークンと表示 URL は
`ajt doctor` が出してくれます。

## iPhone 側の設定

3通りあります。**Tailscale が一番おすすめ**です（理由は下記）。

### 1. Tailscale HTTPS（推奨）

iPhone が PC と別のネットワークにいても届き、しかも **iOS がカメラを許可する
HTTPS** になります。証明書は Let's Encrypt の正規のものなので、警告も出ません。

```bash
ajt serve                       # PC でサーバー起動
tailscale serve --bg --https=443 http://127.0.0.1:8765   # 初回のみ
```

GUI の「Tailscale HTTPS を有効化」ボタンからも設定できます。
表示された QR を iPhone で読むだけで、カメラページが開きます。

- iPhone 側にも Tailscale アプリが必要です（同じ tailnet にログイン）。
- `tailscale serve` はオペレーター権限が必要です。GUI から失敗した場合は上記コマンドを
  一度だけ実行してください。
- PC の IP が変わっても `*.ts.net` 名は変わらないので、QR を作り直す必要がありません。

### 2. IP カメラアプリ方式

アプリが MJPEG / JPEG を配信し、PC がそれを取り込みます。ブラウザのカメラ許可が
不要なため、HTTPS なしでも動きます。

1. App Store で「IP Camera」系アプリ（例: *IP Camera Lite*, *IP Webcam*）を入れます。
2. アプリが表示する URL（例 `http://192.168.0.12:8080/video`）を控えます。
3. PC の `.env` に設定して `ajt serve` を再起動します。

```env
AJT_SOURCE_URL=http://192.168.0.12:8080/video
```

スナップショット JPEG だけを返すアプリ（`.../shot.jpg` 等）でも自動判別してポーリングします。

### 3. LAN 内の HTTPS（自己署名）

Tailscale を使わず、同一 LAN 内だけで完結させる場合です。

```bash
uv run ajt serve --https
```

初回だけ自己署名証明書の警告を承認する必要があります。

> **なぜ HTTPS が必要か**: iOS Safari は安全なコンテキスト（HTTPS）でしか
> `getUserMedia` を許可しません。素の `http://` で開くとカメラボタンが動きません。
> Tailscale を使わない場合は 2 か 3 を選んでください。

## 使い方

- **PC のカメラで試す** — GUI の「カメラ開始」で PC 内蔵カメラを使えます。まずは手元の本で試すのが早いです。
- **画像を読む** — 既に撮ってある写真を「画像を読む」から選べます。
- **原文を直す** — OCR は完全ではありません。「編集して再翻訳」で原文を手直しすると、その場で訳し直します。誤認識を直すのが最短で精度を上げる方法です。
- 検出した行と信頼度は「検出した行」に、重要語の語釈は「語注」に出ます。

## CLI

```bash
ajt serve                     # GUI サーバー
ajt serve --https             # 自己署名証明書で HTTPS（iPhone ブラウザ用）
ajt ocr page.jpg              # 画像 1 枚を OCR して正規化結果まで表示
ajt ocr page.jpg --json       # 行・座標・信頼度つき
ajt translate "いとあはれなりけり"   # 標準入力からでも可
ajt doctor                    # 環境診断とペアリング URL
```

## 翻訳の精度について

**LLM バックエンドを強く推奨します。** 文語の解釈は文脈依存で、規則では限界が
あります。`.env` に以下のいずれかを設定してください。

```env
# OpenAI 互換（DeepSeek やローカルの llama.cpp でも可）
AJT_OPENAI_BASE_URL=https://api.deepseek.com/v1
AJT_OPENAI_API_KEY=sk-...
AJT_OPENAI_MODEL=deepseek-chat

# または Gemini
AJT_GEMINI_API_KEY=...
AJT_GEMINI_MODEL=gemini-2.0-flash
```

`AJT_PROVIDER=offline` にすると、ネットワークなしで規則ベースの訳だけを使います。
この場合アプリは**自信度を低く表示し、注意書きを出します**。規則訳は頻出の文語
語彙・助動詞に対応しますが、未知語は原文のまま残します（でたらめを創作するより
原文のほうが役に立つため）。

翻訳エラー時は自動でオフライン訳に落ちるので、ライブ中に画面が空になることは
ありません。

## 構成

```
src/ajt/
  normalize.py   変体仮名・旧字体・歴史的仮名遣いの正規化
  ocr.py         RapidOCR ラッパ、縦書きの読み順、UI除去、フレーム差分
  translate.py   LLM / オフライン翻訳、JSON抽出、フォールバック
  capture.py     IPカメラの MJPEG / スナップショット取り込み
  pairing.py     LAN IP、QRコード、自己署名証明書
  server.py      FastAPI（フレーム投入・WebSocket・ペアリング）
  cli.py         serve / ocr / translate / doctor
  web/           GUI（index.html）と iPhone 用カメラページ（camera.html）
```

### API

認証は `?t=<token>` か `Authorization: Bearer <token>`。

| メソッド | パス | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 状態（認証不要） |
| POST | `/api/frame` | JPEG を multipart で送る |
| POST | `/api/frame/raw` | JPEG をそのままボディに送る |
| POST | `/api/text` | テキストだけ翻訳 |
| GET | `/api/pair` | QR（SVG）とペアリング URL |
| GET | `/api/last` | 直近の結果 |
| WS | `/ws?t=` | 結果のプッシュ配信 |

IP カメラアプリによっては、そのまま `POST /api/frame/raw` に投げ込めます。

```bash
curl -X POST "http://localhost:8765/api/frame/raw?t=$TOKEN" \
     -H "Content-Type: image/jpeg" --data-binary @page.jpg
```

## テスト

```bash
uv run pytest -q
```

OCR モデルを読み込まずに、読み順・UI除去・正規化・翻訳・HTTP/WebSocket を検証します。

## 制限

- くずし字（草書）の崩しが強い箇所は、汎用 OCR では読み取れません。変体仮名には
  対応しますが、崩し字専用モデルではありません。
- 写真のピント・光量・傾きに敏感です。真上から、影を入れずに撮るのがコツです。
- 縦書きの列検出は行単位の結合に依存します。列が大きく傾くと順序が乱れることがあります。
- オフライン訳は近似です。精度が必要なら LLM を設定してください。

## ライセンス

MIT
