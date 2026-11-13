"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  TeammateConversationInfo,
  TeammateConversationMessage,
  TeammateConversationTaskState,
  TeammateInfo,
} from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

type Props = { teammate: TeammateInfo; projectId: string };

export default function TeammateConversation({ teammate, projectId }: Props) {
  const [conversations, setConversations] = useState<TeammateConversationInfo[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [messages, setMessages] = useState<TeammateConversationMessage[]>([]);
  const [taskState, setTaskState] = useState<TeammateConversationTaskState | null>(null);
  const [draft, setDraft] = useState("");
  const [taskTitle, setTaskTitle] = useState("");
  const [taskDescription, setTaskDescription] = useState("");
  const [acceptanceCriteria, setAcceptanceCriteria] = useState("");
  const [conversionConfirmed, setConversionConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [converting, setConverting] = useState(false);
  const [startingHandoff, setStartingHandoff] = useState(false);
  const [error, setError] = useState("");
  const historyRequest = useRef(0);
  const activeConversationRef = useRef("");
  const activeScopeRef = useRef("");
  const scopeKey = `${teammate.id}:${projectId}`;
  activeScopeRef.current = scopeKey;

  useEffect(() => {
    let current = true;
    activeConversationRef.current = "";
    historyRequest.current += 1;
    setLoading(true);
    setError("");
    setDraft("");
    setConversations([]);
    setMessages([]);
    setTaskState(null);
    setConversationId("");
    setTaskTitle("");
    setTaskDescription("");
    setAcceptanceCriteria("");
    setConversionConfirmed(false);
    setSending(false);
    setConverting(false);
    setStartingHandoff(false);
    void api.listTeammateConversations(teammate.id, projectId).then(async ({ conversations: rows }) => {
      if (!current || activeScopeRef.current !== scopeKey) return;
      setConversations(rows);
      const activeId = rows[0]?.id || "";
      activeConversationRef.current = activeId;
      setConversationId(activeId);
      if (activeId) {
        const history = await api.getTeammateConversationMessages(teammate.id, activeId);
        if (current && activeScopeRef.current === scopeKey && activeConversationRef.current === activeId) {
          setMessages(history.messages);
          setTaskState(history.task);
          setTaskTitle(history.conversation.title);
        }
      }
    }).catch((reason) => {
      if (current && activeScopeRef.current === scopeKey) setError(reason instanceof Error ? reason.message : "Could not load conversation history.");
    }).finally(() => {
      if (current && activeScopeRef.current === scopeKey) setLoading(false);
    });
    return () => {
      current = false;
      if (activeScopeRef.current === scopeKey) activeConversationRef.current = "";
    };
  }, [teammate.id, projectId, scopeKey]);

  async function selectConversation(id: string) {
    if (sending || converting || startingHandoff) return;
    const requestScope = scopeKey;
    const requestId = ++historyRequest.current;
    activeConversationRef.current = id;
    setConversationId(id);
    setMessages([]);
    setTaskState(null);
    setTaskTitle("");
    setTaskDescription("");
    setAcceptanceCriteria("");
    setConversionConfirmed(false);
    setDraft("");
    setError("");
    if (!id) return;
    setLoading(true);
    try {
      const history = await api.getTeammateConversationMessages(teammate.id, id);
      if (historyRequest.current === requestId && activeScopeRef.current === requestScope && activeConversationRef.current === id) {
        setMessages(history.messages);
        setTaskState(history.task);
        setTaskTitle(history.conversation.title);
      }
    } catch (reason) {
      if (historyRequest.current === requestId && activeScopeRef.current === requestScope && activeConversationRef.current === id) {
        setError(reason instanceof Error ? reason.message : "Could not load conversation history.");
      }
    } finally {
      if (historyRequest.current === requestId && activeScopeRef.current === requestScope && activeConversationRef.current === id) setLoading(false);
    }
  }

  async function startConversation() {
    if (sending || converting || startingHandoff) return;
    const requestScope = scopeKey;
    setLoading(true);
    setError("");
    try {
      const conversation = await api.createTeammateConversation(teammate.id, projectId);
      if (activeScopeRef.current !== requestScope) return;
      historyRequest.current += 1;
      activeConversationRef.current = conversation.id;
      setConversations((rows) => [conversation, ...rows]);
      setConversationId(conversation.id);
      setMessages([]);
      setTaskState(null);
      setDraft("");
      setTaskTitle("");
      setTaskDescription("");
      setAcceptanceCriteria("");
      setConversionConfirmed(false);
    } catch (reason) {
      if (activeScopeRef.current === requestScope) setError(reason instanceof Error ? reason.message : "Could not start a conversation.");
    } finally {
      if (activeScopeRef.current === requestScope) setLoading(false);
    }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!conversationId || !content || sending || converting || startingHandoff) return;
    const targetConversationId = conversationId;
    const requestScope = scopeKey;
    setSending(true);
    setError("");
    try {
      const result = await api.sendTeammateConversationMessage(teammate.id, targetConversationId, content);
      if (activeScopeRef.current !== requestScope || activeConversationRef.current !== targetConversationId) return;
      setMessages((current) => [...current, ...result.messages]);
      setConversations((current) => current.map((row) => row.id === targetConversationId ? result.conversation : row));
      setDraft("");
    } catch (reason) {
      if (activeScopeRef.current === requestScope && activeConversationRef.current === targetConversationId) {
        setError(reason instanceof Error ? reason.message : "Could not send this message.");
      }
    } finally {
      if (activeScopeRef.current === requestScope) setSending(false);
    }
  }

  async function convertConversation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!conversationId || converting || startingHandoff || !conversionConfirmed) return;
    const targetConversationId = conversationId;
    const requestScope = scopeKey;
    const criteria = acceptanceCriteria.split("\n").map((item) => item.trim()).filter(Boolean);
    setConverting(true);
    setError("");
    try {
      const result = await api.convertTeammateConversationToTask(teammate.id, targetConversationId, {
        confirmed: true,
        title: taskTitle.trim(),
        description: taskDescription.trim(),
        acceptance_criteria: criteria,
      });
      if (activeScopeRef.current !== requestScope || activeConversationRef.current !== targetConversationId) return;
      setTaskState(result.task);
      setConversations((rows) => rows.map((row) => row.id === targetConversationId ? result.conversation : row));
    } catch (reason) {
      if (activeScopeRef.current === requestScope && activeConversationRef.current === targetConversationId) {
        setError(reason instanceof Error ? reason.message : "Could not create the approval-gated task.");
      }
    } finally {
      if (activeScopeRef.current === requestScope) setConverting(false);
    }
  }

  async function startApprovedHandoff() {
    const activeConversation = conversations.find((row) => row.id === conversationId);
    if (
      sending || converting || loading || startingHandoff ||
      !activeConversation || activeConversation.teammate_id !== teammate.id || activeConversation.project_id !== projectId ||
      !activeConversation.root_task_id || !taskState || taskState.root_task_id !== activeConversation.root_task_id ||
      !taskState.idempotency_key || !taskState.reason || !taskState.input_data ||
      taskState.approval_status !== "approved" || taskState.status !== "pending" || taskState.handoff_status !== "pending"
    ) return;
    const targetConversationId = conversationId;
    const requestScope = scopeKey;
    const currentTaskState = taskState;
    if (!targetConversationId) return;
    setStartingHandoff(true);
    setError("");
    try {
      await api.createTaskHandoff(currentTaskState.execution_id, currentTaskState.root_task_id, {
        idempotency_key: currentTaskState.idempotency_key,
        target_teammate_id: teammate.id,
        project_id: projectId,
        reason: currentTaskState.reason,
        input_data: currentTaskState.input_data,
        requires_approval: true,
      });
      const history = await api.getTeammateConversationMessages(teammate.id, targetConversationId);
      if (activeScopeRef.current === requestScope && activeConversationRef.current === targetConversationId) setTaskState(history.task);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Could not start the approved task.";
      if (activeScopeRef.current !== requestScope || activeConversationRef.current !== targetConversationId) return;
      setError(message);
      if (message.toLowerCase().includes("acceptance gates are incomplete") && currentTaskState.confirmed_details) {
        try {
          const details = currentTaskState.confirmed_details;
          await api.convertTeammateConversationToTask(teammate.id, targetConversationId, {
            confirmed: true,
            title: details.title,
            description: details.description,
            acceptance_criteria: details.acceptance_criteria,
          });
        } catch {
          // Refresh below still gives the user the latest durable task state.
        }
      }
      try {
        const history = await api.getTeammateConversationMessages(teammate.id, targetConversationId);
        if (activeScopeRef.current === requestScope && activeConversationRef.current === targetConversationId) setTaskState(history.task);
      } catch {
        // Keep the handoff failure visible if the refresh also fails.
      }
    } finally {
      if (activeScopeRef.current === requestScope) setStartingHandoff(false);
    }
  }

  const navigationBusy = loading || sending || converting || startingHandoff;
  const activeConversation = conversations.find((row) => row.id === conversationId);
  const canStartApprovedHandoff = Boolean(
    activeConversation &&
    activeConversation.teammate_id === teammate.id &&
    activeConversation.project_id === projectId &&
    activeConversation.root_task_id &&
    taskState &&
    taskState.root_task_id === activeConversation.root_task_id &&
    taskState.approval_status === "approved" &&
    taskState.status === "pending" &&
    taskState.handoff_status === "pending"
  );

  return (
    <div className={styles.teammateRunBody}>
      <div className={styles.formGrid}>
        <label className="field">
          <span className="field-label">Conversation history</span>
          <select className="editorial-input" disabled={navigationBusy} onChange={(event) => void selectConversation(event.target.value)} value={conversationId}>
            <option value="">Choose a conversation</option>
            {conversations.map((conversation) => <option key={conversation.id} value={conversation.id}>{conversation.title}</option>)}
          </select>
        </label>
        <div className={styles.formFooter}>
          <button className="action-button secondary" disabled={navigationBusy} onClick={() => void startConversation()} type="button">New conversation</button>
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
          <textarea className="editorial-input" disabled={navigationBusy} maxLength={10_000} onChange={(event) => setDraft(event.target.value)} placeholder="Continue a project-specific engineering conversation" rows={3} value={draft} />
        </label>
        <div className={styles.formFooter}>
          <span className="copy-muted">Conversation messages are saved and used as bounded context on later turns. This chat does not grant workspace tools.</span>
          <button className="action-button" disabled={!conversationId || !draft.trim() || navigationBusy} type="submit">{sending ? "Waiting for response…" : "Send message"}</button>
        </div>
      </form>
      {messages.some((message) => message.role === "user") && !taskState && (
        <form className={styles.form} onSubmit={convertConversation}>
          <h3>Turn this conversation into a task</h3>
          <p className="copy-muted">This creates a durable task using the saved chat as bounded context. A project owner or admin must approve before the teammate receives permission-bound workspace tools.</p>
          <label className="field">
            <span className="field-label">Task title</span>
            <input className="editorial-input" disabled={navigationBusy} maxLength={255} onChange={(event) => setTaskTitle(event.target.value)} required value={taskTitle} />
          </label>
          <label className="field">
            <span className="field-label">Confirmed task description</span>
            <textarea className="editorial-input" disabled={navigationBusy} maxLength={10_000} onChange={(event) => setTaskDescription(event.target.value)} required rows={4} value={taskDescription} />
          </label>
          <label className="field">
            <span className="field-label">Acceptance criteria (one per line)</span>
            <textarea className="editorial-input" disabled={navigationBusy} maxLength={10_000} onChange={(event) => setAcceptanceCriteria(event.target.value)} placeholder="The requested behavior works\nChanges are verified" required rows={3} value={acceptanceCriteria} />
          </label>
          <label className="field">
            <span className="field-label">
              <input checked={conversionConfirmed} disabled={navigationBusy} onChange={(event) => setConversionConfirmed(event.target.checked)} type="checkbox" /> I confirm these details should become a task and be sent for owner/admin approval.
            </span>
          </label>
          <div className={styles.formFooter}>
            <span className="copy-muted">No workspace tools are granted until approval.</span>
            <button className="action-button" disabled={!conversionConfirmed || navigationBusy || !taskTitle.trim() || !taskDescription.trim() || !acceptanceCriteria.trim()} type="submit">{converting ? "Creating task…" : "Create task for approval"}</button>
          </div>
        </form>
      )}
      {taskState && (
        <section aria-label="Conversation task status" className={styles.form}>
          <h3>Task status: {taskState.status.replaceAll("_", " ")}</h3>
          {taskState.handoff_status && <p className="copy-muted">Handoff: {taskState.handoff_status.replaceAll("_", " ")}</p>}
          {taskState.approval_id && taskState.approval_status === "pending" && <p className="copy-muted">Waiting for an owner or admin to review this handoff.</p>}
          {taskState.approval_id && canStartApprovedHandoff && (
            <div className={styles.formFooter}>
              <p className="copy-muted">Approval recorded. Start the lease-fenced teammate handoff when ready.</p>
              <button className="action-button" disabled={navigationBusy} onClick={() => void startApprovedHandoff()} type="button">{startingHandoff ? "Starting…" : "Start approved task"}</button>
            </div>
          )}
          {taskState.approval_id && taskState.approval_status === "pending" && <Link className="action-button secondary" href="/approvals">Review approvals</Link>}
          {taskState.task_id && <Link className="action-button secondary" href={`/tasks/${taskState.execution_id}/${taskState.task_id}`}>Open task review</Link>}
        </section>
      )}
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
