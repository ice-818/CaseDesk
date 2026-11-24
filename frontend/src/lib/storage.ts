import type { Conversation, Message } from "../types";
import { getAuthScope, getCredentials, isCurrentAuth } from "./auth";

const MAX_CONVERSATIONS = 50;
const MAX_MESSAGES = 100;

export function captureStorageScope() {
  const credentials = getCredentials();
  const authScope = getAuthScope();
  if (!credentials || !authScope) throw new Error("登录并确认有效范围后才能读取账号缓存");
  return Object.freeze({ userId: credentials.user.user_id, version: credentials.version, scopeId: authScope.key });
}
type Scope = ReturnType<typeof captureStorageScope>;

function cacheKey(scope: Scope): string | null {
  const credentials = getCredentials();
  const authScope = getAuthScope();
  if (!isCurrentAuth(scope.version) || credentials?.user.user_id !== scope.userId || !authScope || scope.scopeId !== authScope.key) return null;
  // v1/v2 caches remain untouched; their original role/shop cannot be proven.
  return `casedesk-web:v3:${encodeURIComponent(scope.userId)}:${encodeURIComponent(scope.scopeId)}`;
}

export function loadTheme(scope: Scope): "light" | "dark" {
  try { const key = cacheKey(scope); return key && localStorage.getItem(`${key}:theme`) === "dark" ? "dark" : "light"; }
  catch { return "light"; }
}

export function saveTheme(theme: "light" | "dark", scope: Scope): void {
  try { const key = cacheKey(scope); if (key) localStorage.setItem(`${key}:theme`, theme); }
  catch { /* Disabled/full storage does not prevent using the workspace. */ }
}

export function loadConversations(scope: Scope): Conversation[] {
  try {
    const key = cacheKey(scope);
    const raw = key ? localStorage.getItem(key) : null;
    if (!raw) return [];
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    const conversations = value.filter(isConversation).slice(0, MAX_CONVERSATIONS).map((item) => ({
      ...item,
      messages: item.messages.slice(-MAX_MESSAGES).map((message) => ({
        ...message,
        pendingAction: null,
        state: message.state === "streaming" ? "stopped" : message.state,
      })),
    }));
    return conversations;
  } catch {
    return [];
  }
}

export function saveConversations(conversations: Conversation[], scope: Scope): void {
  try {
    const key = cacheKey(scope);
    if (!key) return;
    localStorage.setItem(
      key,
      JSON.stringify(conversations.slice(0, MAX_CONVERSATIONS).map((item) => ({
        ...item,
        messages: item.messages.slice(-MAX_MESSAGES).map(message => ({ ...message, pendingAction: null })),
      }))),
    );
  } catch { /* Storage may be disabled or full; the current chat still works. */ }
}

function isConversation(value: unknown): value is Conversation {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return typeof item.id === "string" && typeof item.title === "string" &&
    typeof item.updatedAt === "number" && Array.isArray(item.messages) &&
    item.messages.every(isMessage);
}

function isMessage(value: unknown): value is Message {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return typeof item.id === "string" &&
    (item.role === "user" || item.role === "assistant") &&
    typeof item.content === "string" && typeof item.state === "string";
}
