"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import {
  api,
  TeammateConversationInfo,
  TeammateConversationMessage,
  TeammateInfo,
} from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

type Props = { teammate: TeammateInfo; projectId: string };

export default function TeammateConversation({ teammate, projectId }: Props) {
  const [conversations, setConversations] = useState<TeammateConversationInfo[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [messages, setMessages] = useState<TeammateConversationMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const historyRequest = useRef(0);

  useEffect(() => {
    let current = true;
    historyRequest.current += 1;
    setLoading(true);
    setError("");
    setMessages([]);
    setConversationId("");
    void api.listTeammateConversations(teammate.id, projectId).then(async ({ conversations: rows }) => {
      if (!current) return;
      setConversations(rows);
      const activeId = rows[0]?.id || "";
      setConversationId(activeId);
      if (activeId) {
        const history = await api.getTeammateConversationMessages(teammate.id, activeId);
        if (current) setMessages(history.messages);
      }
    }).catch((reason) => {
      if (current) setError(reason instanceof Error ? reason.message : "Could not load conversation history.");
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, [teammate.id, projectId]);

  async function selectConversation(id: string) {
    const requestId = ++historyRequest.current;
    setConversationId(id);
    setMessages([]);
    setError("");
    if (!id) return;
    setLoading(true);
    try {
      const history = await api.getTeammateConversationMessages(teammate.id, id);
      if (historyRequest.current === requestId) setMessages(history.messages);
    } catch (reason) {
      if (historyRequest.current === requestId) {
        setError(reason instanceof Error ? reason.message : "Could not load conversation history.");
      }
    } finally {
      if (historyRequest.current === requestId) setLoading(false);
    }
  }

  async function startConversation() {
    setLoading(true);
    setError("");
    try {
      const conversation = await api.createTeammateConversation(teammate.id, projectId);
      setConversations((rows) => [conversation, ...rows]);
      setConversationId(conversation.id);
      setMessages([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start a conversation.");
    } finally {
      setLoading(false);
    }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!conversationId || !content || sending) return;
    setSending(true);
    setError("");
    try {
      const result = await api.sendTeammateConversationMessage(teammate.id, conversationId, content);
      setMessages((current) => [...current, ...result.messages]);
      setConversations((current) => current.map((row) => row.id === conversationId ? result.conversation : row));
      setDraft("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not send this message.");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className={styles.teammateRunBody}>
      <div className={styles.formGrid}>
        <label className="field">
          <span className="field-label">Conversation history</span>
          <select className="editorial-input" disabled={loading || sending} onChange={(event) => void selectConversation(event.target.value)} value={conversationId}>
            <option value="">Choose a conversation</option>
            {conversations.map((conversation) => <option key={conversation.id} value={conversation.id}>{conversation.title}</option>)}
          </select>
        </label>
        <div className={styles.formFooter}>
          <button className="action-button secondary" disabled={loading || sending} onClick={() => void startConversation()} type="button">New conversation</button>
        </div>
      </div>
      {messages.length > 0 && (
        <ol aria-label="Conversation messages" className={styles.teammateMessages}>
          {messages.map((message) => (
            <li key={message.id}>
              <strong>{message.role === "user" ? "You" : teammate.name}</strong>
              <pre>{formatMessage(message.content)}</pre>
            </li>
          ))}
        </ol>
      )}
      <form className={styles.form} onSubmit={sendMessage}>
        <label className="field">
          <span className="field-label">Message</span>
          <textarea className="editorial-input" maxLength={10_000} onChange={(event) => setDraft(event.target.value)} placeholder="Continue a project-specific engineering conversation" rows={3} value={draft} />
        </label>
        <div className={styles.formFooter}>
          <span className="copy-muted">Conversation messages are saved and used as bounded context on later turns. This chat does not grant workspace tools.</span>
          <button className="action-button" disabled={!conversationId || !draft.trim() || sending || loading} type="submit">{sending ? "Waiting for response…" : "Send message"}</button>
        </div>
      </form>
      {loading && <p className="copy-muted" role="status">Loading conversation…</p>}
      {error && <p className={styles.error} role="alert">{error}</p>}
    </div>
  );
}

function formatMessage(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}
