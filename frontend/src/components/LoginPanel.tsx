import { useState } from "react";
import { useAuth } from "../auth/AuthProvider";
import { Logo } from "./Logo";

export function LoginPanel() {
  const auth = useAuth();
  const [userId, setUserId] = useState("");
  const [password, setPassword] = useState("");
  const busy = auth.status === "signing_in";

  async function submit() {
    if (busy || !userId.trim() || !password) return;
    const pending = auth.login(userId.trim(), password);
    setPassword("");
    await pending;
  }

  return <main className="login-page"><section className="login-panel">
    <Logo /><h1>登录 CaseDesk</h1><p>电商智能客服与售后工作台</p>
    {auth.notice && <p className="auth-notice" role="alert">{auth.notice}</p>}
    <form autoComplete="off" onSubmit={e => { e.preventDefault(); void submit(); }}>
      <label htmlFor="login-user">账号</label>
      <input id="login-user" name="user_id" value={userId} maxLength={64} required disabled={busy} autoComplete="off" autoCapitalize="none" spellCheck={false} onChange={e => setUserId(e.target.value)} />
      <label htmlFor="login-password">密码</label>
      <input id="login-password" name="password" type="password" value={password} required disabled={busy} autoComplete="off" onChange={e => setPassword(e.target.value)} />
      <button type="submit" disabled={busy || !userId.trim() || !password}>{busy ? "正在登录…" : "登录"}</button>
    </form>
    <small>刷新页面后需重新登录。</small>
  </section></main>;
}
