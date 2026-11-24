import { createContext, useContext, useEffect, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { beginLogin, clearAuth, completeLogin, getAuthSnapshot, getCredentials, invalidateAuth, setAuthNotice, subscribeAuth } from "../lib/auth";
import type { AuthSnapshot } from "../lib/auth";
import { loginRequest, logoutRequest } from "../lib/api";

interface AuthContextValue extends AuthSnapshot {
  login: (userId: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const state = useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthSnapshot);

  useEffect(() => {
    if (state.status !== "signed_in" || !state.expiresAt) return;
    // Checking the local deadline also covers idle pages; the server remains authoritative.
    const check = () => {
      if (Date.now() >= Date.parse(state.expiresAt!)) invalidateAuth(state.version);
    };
    check();
    const timer = window.setInterval(check, 1000);
    return () => window.clearInterval(timer);
  }, [state.status, state.version, state.expiresAt]);

  async function login(userId: string, password: string): Promise<boolean> {
    const attempt = beginLogin();
    try {
      const result = await loginRequest(userId, password, attempt.signal);
      return completeLogin(attempt.version, result);
    } catch {
      clearAuth(attempt.version, "登录失败，请检查账号和密码或稍后重试。");
      return false;
    }
  }

  async function logout() {
    const credentials = getCredentials();
    if (!credentials) { clearAuth(); return; }
    // Capture credentials first, then cancel business requests immediately. The revoke
    // request has its own lifetime so clearing local identity cannot cancel logout.
    const revoke = logoutRequest(credentials);
    clearAuth(credentials.version);
    const signedOutVersion = getAuthSnapshot().version;
    try { await revoke; }
    catch { setAuthNotice(signedOutVersion, "已退出本机登录，但未能确认服务端令牌撤销，请稍后重试登录。"); }
  }

  return <AuthContext.Provider value={{ ...state, login, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider is required");
  return value;
}
