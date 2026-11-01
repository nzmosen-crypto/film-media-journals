# 電影與媒體研究期刊 · 最新一期 / Film & Media Studies Journals — Latest Issues

每月自動抓取 42 本英語電影與媒體研究期刊的最新一期目錄與摘要，發佈成公開網頁與 RSS。
Every month this repository collects the latest-issue tables of contents (with abstracts) of 42 English-language film and media studies journals and publishes them as a web page and an RSS feed.

## 訂閱 / Subscribe

- **網頁 / Web page**：本 repo 的 GitHub Pages（Settings → Pages 顯示網址）
- **RSS**：網頁網址後加 `feed.xml`。加到 Feedly、Inoreader 等閱讀器，任一期刊出新一期就會收到一則。
- **Email**：把 RSS 網址貼到 Blogtrottr、Feedrabbit 等免費 RSS-to-email 服務。

## 期刊 / Journals (`journals.csv`)

選刊條件：英語、同儕審查；SCImago (SJR) 電影／傳播／文化研究類 Q1 為主，另收 *October*、*Grey Room* 等具高度學術聲望的期刊。
Selection: English-language, peer-reviewed; mostly SJR Q1 in film, communication or cultural studies, plus highly regarded journals such as *October* and *Grey Room*.

## 資料來源 / Data sources

Crossref；摘要由 OpenAlex 補齊。部分出版社登記期別較慢，新文章會列在「Online first」。

## 修改 / Editing

- 增刪期刊：編輯 `journals.csv`，存檔後會自動重跑。
- 手動執行：Actions → *Update film and media journals* → Run workflow。
- 寄信給自己（選用）：在 Settings → Secrets → Actions 加 `GMAIL_USER`、`GMAIL_APP_PASSWORD`、`MAIL_TO`。
