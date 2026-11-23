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
