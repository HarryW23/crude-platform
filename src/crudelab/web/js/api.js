// 所有後端調用都經過這裡。出錯時拋出帶中文訊息的 Error。
export async function api(path, options = {}) {
  const init = { ...options };
  if (init.json !== undefined) {
    init.body = JSON.stringify(init.json);
    init.headers = { "Content-Type": "application/json" };
    init.method ??= "POST";
  }
  const response = await fetch("/api" + path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body));
  }
  return body;
}
