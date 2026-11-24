# crudelab：給 Claude Code 的倉庫約定

## 提交署名

提交由倉庫主人和 Claude 一起署名，貢獻算在倉庫主人的 GitHub 帳號上：

- 作者和提交者用倉庫主人的 GitHub noreply 地址（不要用個人郵箱，倉庫是公開的）：

  ```bash
  git config user.name "Harry Wang"
  git config user.email "355297872+HarryW23@users.noreply.github.com"
  ```

  每次新開的環境都要先設一次（寫在倉庫的 .git/config 裡，不改全局設定）。
- 提交說明末尾照常帶上 Claude 的 `Co-Authored-By` 行。
- 不要改寫已經推上去的歷史。

## 測試

- 提交前先跑測試，失敗不提交。快的跑法：`python -m pytest -n auto -m "not serial" && python -m pytest -m serial`
  （多核並行；標了 `serial` 的會起真實服務進程、佔端口，單獨順序跑）。CI 也是這樣跑
- 新測試要起進程、佔端口或依賴真實等待時間：標 `@pytest.mark.serial`；需要空庫（不用遷移好的模板）：標 `@pytest.mark.empty_db`
- 測試只能寫臨時目錄（tmp_path），不能寫項目裡的文件：並行時會互相覆蓋
