import { useEffect, useRef, useState } from "react";
import { ArrowDown, CircleHelp, Menu, PanelLeftOpen } from "lucide-react";
import { checkHealth } from "./lib/api";
import { useChat } from "./hooks/useChat";
import { Composer } from "./components/Composer";
import { Logo } from "./components/Logo";
import { MessageView } from "./components/MessageView";
import { SearchDialog } from "./components/SearchDialog";
import { Sidebar } from "./components/Sidebar";
import { ReceptionPanel } from "./components/ReceptionPanel";
import { PlatformPanel } from "./components/PlatformPanel";
import { EvolutionPanel } from "./components/EvolutionPanel";
import type { ActionProposal } from "./types";
import { useAuth } from "./auth/AuthProvider";
import { LoginPanel } from "./components/LoginPanel";
import { captureStorageScope, loadTheme, saveTheme } from "./lib/storage";

const SUGGESTIONS = [
  { icon: "↩", title: "退换货政策", prompt: "定制商品过了七天，但有质量问题还能退吗？" },
  { icon: "□", title: "查询订单", prompt: "帮我查一下 ORD10001 的物流进度" },
  { icon: "¥", title: "支付与发票", prompt: "电子发票一般多久可以开具？" },
  { icon: "✓", title: "发起售后", prompt: "退货申请需要提供哪些信息？" },
];

function App() {
  const auth = useAuth();
  useEffect(() => {
    if (auth.status !== "signed_in") document.documentElement.dataset.theme = "light";
  }, [auth.status]);
  if (auth.status !== "signed_in" || !auth.user) return <LoginPanel />;
  return <Workspace key={auth.version} />;
}

function Workspace() {
  const auth = useAuth();
  const [storageScope] = useState(captureStorageScope);
  const chat = useChat();
  const [platformOpen, setPlatformOpen] = useState(false);
  const [evolutionOpen, setEvolutionOpen] = useState(false);
  const [receptionState, setReceptionState] = useState<{ sessionId: string; closed: boolean } | null>(null);
  const receptionClosed = Boolean(chat.active?.sessionId && receptionState?.sessionId === chat.active.sessionId && receptionState.closed);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [theme, setTheme] = useState<"light" | "dark">(() => loadTheme(storageScope));
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    saveTheme(theme, storageScope);
  }, [theme]);
  useEffect(() => {
    const controller = new AbortController();
    void checkHealth(controller.signal).then(setHealthy);
    return () => controller.abort();
  }, []);
  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setSearchOpen(true); }
      if (event.key === "Escape") { setSearchOpen(false); setSidebarOpen(false); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function newChat() {
    chat.select(null);
    setDraft("");
    setSidebarOpen(false);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }

  function selectChat(id: string) { chat.select(id); setSidebarOpen(false); setSearchOpen(false); }
  function suggest(prompt: string) { setDraft(prompt); inputRef.current?.focus(); }
  function confirm(action: ActionProposal, messageId: string) {
    if (!chat.active || receptionClosed) return;
    const conversationId = chat.active.id;
    void chat.send("确认执行该操作", action.action_id).then((succeeded) => {
      if (succeeded) chat.dismissAction(conversationId, messageId);
    });
  }

  if (platformOpen && auth.user?.role === "operator") return <PlatformPanel onBack={() => setPlatformOpen(false)} />;
  if (evolutionOpen && auth.user?.role === "operator") return <EvolutionPanel onBack={() => setEvolutionOpen(false)} />;
  return <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
    <Sidebar conversations={chat.conversations} activeId={chat.active?.id ?? null} open={sidebarOpen} onClose={() => { setSidebarOpen(false); setSidebarCollapsed(true); }} onNew={newChat} onSelect={selectChat} onRemove={chat.remove} onSearch={() => setSearchOpen(true)} onTheme={() => setTheme(theme === "light" ? "dark" : "light")} theme={theme} />
    <main className="main-panel">
      <header className="topbar">
        <button className="icon-button open-sidebar" aria-label="打开侧栏" title="打开侧栏" onClick={() => { setSidebarCollapsed(false); setSidebarOpen(true); }}>{sidebarCollapsed ? <PanelLeftOpen size={20} /> : <Menu size={20} />}</button>
        <span className="model-title">CaseDesk <small>智能客服和导购平台</small></span>
        {auth.user?.role === "operator" && <button onClick={() => setPlatformOpen(true)} disabled={chat.busy}>数据与评测</button>}
        {auth.user?.role === "operator" && <button onClick={() => setEvolutionOpen(true)} disabled={chat.busy}>自进化人审</button>}
        <span className="account-name">{auth.user?.user_id}</span><button onClick={() => void auth.logout()}>退出登录</button>
        <div className="topbar-right"><span className={`connection-pill ${healthy === false ? "offline" : ""}`}><span />{healthy === null ? "正在检查服务" : healthy ? "服务已连接" : "服务未连接"}</span><button className="icon-button help-button" title="项目说明" aria-label="项目说明" onClick={() => window.open("https://github.com/ice-818/CaseDesk", "_blank", "noopener,noreferrer")}><CircleHelp size={19} /></button></div>
      </header>
      {healthy === false && <div className="service-banner" role="status"><strong>暂时无法连接服务</strong><span>请启动 CaseDesk API 后重试。</span></div>}
      {chat.active ? <div className="chat-content"><MessageView conversation={chat.active} busy={chat.busy || receptionClosed} onConfirm={confirm} onCancel={(messageId) => chat.dismissAction(chat.active!.id, messageId)} onNavigate={(text) => void chat.send(text)} /></div>
        : <div className="welcome"><div className="welcome-inner"><Logo /><span className="welcome-kicker">CASE MANAGEMENT</span><h1>今天要处理哪一件客户诉求？</h1><p>在同一个工作台查询政策、跟进订单并处理售后，让每一次答复都有据可查。</p><div className="suggestions">{SUGGESTIONS.map((item) => <button key={item.title} onClick={() => suggest(item.prompt)}><span className="suggestion-icon">{item.icon}</span><span><strong>{item.title}</strong><small>{item.prompt}</small></span><ArrowDown size={15} /></button>)}</div></div></div>}
      {chat.active?.sessionId && <ReceptionPanel key={chat.active.sessionId} sessionId={chat.active.sessionId} busy={chat.busy} onClosed={closed => setReceptionState({ sessionId: chat.active!.sessionId!, closed })} onNew={newChat} />}
      {!receptionClosed && <Composer busy={chat.busy} onSend={(message) => void chat.send(message)} onStop={chat.stop} draft={draft} onDraft={setDraft} inputRef={inputRef} />}
    </main>
    {searchOpen && <SearchDialog conversations={chat.conversations} onClose={() => setSearchOpen(false)} onSelect={selectChat} />}
  </div>;
}

export default App;
