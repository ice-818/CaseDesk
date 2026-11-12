import type { FormEvent, KeyboardEvent, RefObject } from "react";
import { ArrowUp, Square } from "lucide-react";

interface ComposerProps {
  busy: boolean;
  onSend: (message: string) => void;
  onStop: () => void;
  draft: string;
  onDraft: (value: string) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
}

export function Composer({ busy, onSend, onStop, draft, onDraft, inputRef }: ComposerProps) {
  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!busy && draft.trim()) { onSend(draft); onDraft(""); }
  }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }
  return <div className="composer-wrap">
    <form className="composer" onSubmit={submit}>
      <textarea ref={inputRef} value={draft} onChange={(event) => { onDraft(event.target.value); event.target.style.height = "auto"; event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`; }} onKeyDown={keyDown} placeholder="输入客户问题、订单号或需要处理的事项…" rows={1} maxLength={4000} aria-label="输入客户事项" />
      <div className="composer-bottom"><span className="composer-hint">Enter 发送 · Shift + Enter 换行</span>
        {busy ? <button type="button" className="send-button stop-button" aria-label="停止生成" title="停止生成" onClick={onStop}><Square size={15} fill="currentColor" /></button>
          : <button type="button" className="send-button" aria-label="发送消息" title="发送消息" disabled={!draft.trim()} onClick={() => submit()}><ArrowUp size={19} strokeWidth={2.5} /></button>}
      </div>
    </form>
    <p className="composer-disclaimer">CaseDesk 会引用可用来源；涉及退款、取消等操作仍需你的确认。</p>
  </div>;
}
