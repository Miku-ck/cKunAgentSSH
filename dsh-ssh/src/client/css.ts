/**
 * Panel styles. Scoped under .ckun-ssh-root, colored through the dsw alias
 * theme tokens so the panel follows light/dark without extra work.
 */
export const CSS_TEXT = `
.ckun-ssh-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--dsw-alias-bg-base, #111318);
  color: var(--dsw-alias-label-primary, #e6e8ee);
  font-size: 13px;
}
.ckun-ssh-body { display: flex; flex: 1; min-height: 0; }
.ckun-ssh-hosts {
  width: 250px; flex: none; display: flex; flex-direction: column; min-height: 0;
  border-right: 1px solid var(--dsw-alias-border-l1, #262a33);
  background: var(--dsw-alias-bg-layer-1, #171a21);
}
.ckun-ssh-hosts-head {
  display: flex; align-items: center; gap: 6px; padding: 10px 12px 8px;
  font-weight: 600;
}
.ckun-ssh-hosts-head .ckun-ssh-spacer { flex: 1; }
.ckun-ssh-search {
  margin: 0 12px 8px; padding: 5px 8px; font-size: 12px;
  background: var(--dsw-alias-bg-layer-2, #1d212b);
  border: 1px solid var(--dsw-alias-border-l1, #262a33);
  border-radius: 6px; color: inherit; outline: none;
}
.ckun-ssh-hosts-list { flex: 1; overflow-y: auto; padding: 0 8px 12px; }
.ckun-ssh-group-title {
  padding: 8px 4px 4px; font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--dsw-alias-label-secondary, #9aa1b0);
}
.ckun-ssh-host-row {
  display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px;
  cursor: pointer; user-select: none;
}
.ckun-ssh-host-row:hover { background: var(--dsw-alias-bg-layer-2, #1d212b); }
.ckun-ssh-host-row.active { background: var(--dsw-alias-bg-overlay, #232837); }
.ckun-ssh-host-row .ckun-ssh-name { font-weight: 600; }
.ckun-ssh-host-row .ckun-ssh-target { color: var(--dsw-alias-label-secondary, #9aa1b0); font-size: 11px; }
.ckun-ssh-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.ckun-ssh-dot.open { background: var(--dsw-alias-state-success-primary, #3fb950); }
.ckun-ssh-dot.connecting { background: var(--dsw-alias-state-warn-primary, #d29922); }
.ckun-ssh-dot.closed { background: var(--dsw-alias-state-idle-primary, #6e7687); }
.ckun-ssh-dot.prod { background: var(--dsw-alias-state-error-primary, #f85149); }
.ckun-ssh-badge {
  font-size: 10px; padding: 1px 6px; border-radius: 8px; flex: none;
  border: 1px solid var(--dsw-alias-border-l2, #333947);
  color: var(--dsw-alias-label-secondary, #9aa1b0);
}
.ckun-ssh-badge.prod { color: var(--dsw-alias-state-error-primary, #f85149); border-color: currentColor; }
.ckun-ssh-badge.warn { color: var(--dsw-alias-state-warn-primary, #d29922); border-color: currentColor; }
.ckun-ssh-main { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.ckun-ssh-subtabs {
  display: flex; align-items: center; gap: 2px; padding: 8px 12px 0;
  border-bottom: 1px solid var(--dsw-alias-border-l1, #262a33);
}
.ckun-ssh-subtab {
  padding: 6px 12px; cursor: pointer; border-radius: 6px 6px 0 0; font-size: 12px;
  color: var(--dsw-alias-label-secondary, #9aa1b0);
}
.ckun-ssh-subtab:hover { color: var(--dsw-alias-label-primary, #e6e8ee); }
.ckun-ssh-subtab.active {
  color: var(--dsw-alias-label-primary, #e6e8ee);
  background: var(--dsw-alias-bg-base, #111318);
  box-shadow: inset 0 2px 0 var(--dsw-alias-brand-primary, #4c82f7);
}
.ckun-ssh-subtabs .ckun-ssh-hosttitle {
  margin-left: 12px; font-weight: 600; font-size: 12px;
  display: flex; align-items: center; gap: 6px;
}
.ckun-ssh-content { flex: 1; min-height: 0; position: relative; }
.ckun-ssh-empty {
  height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px;
  color: var(--dsw-alias-label-secondary, #9aa1b0);
}
.ckun-ssh-statusbar {
  display: flex; align-items: center; gap: 14px; padding: 4px 12px;
  border-top: 1px solid var(--dsw-alias-border-l1, #262a33);
  font-size: 11px; color: var(--dsw-alias-label-secondary, #9aa1b0);
  background: var(--dsw-alias-bg-layer-1, #171a21);
}
/* buttons */
.ckun-ssh-btn {
  display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px;
  background: var(--dsw-alias-bg-layer-2, #1d212b); color: inherit;
  border: 1px solid var(--dsw-alias-border-l2, #333947); border-radius: 6px; cursor: pointer;
}
.ckun-ssh-btn:hover { border-color: var(--dsw-alias-brand-primary, #4c82f7); }
.ckun-ssh-btn.primary { background: var(--dsw-alias-brand-primary, #4c82f7); border-color: transparent; color: #fff; }
.ckun-ssh-btn.danger { color: var(--dsw-alias-state-error-primary, #f85149); }
.ckun-ssh-btn:disabled { opacity: .5; cursor: default; }
.ckun-ssh-icon-btn {
  width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; color: inherit; cursor: pointer; border-radius: 4px; font-size: 14px;
}
.ckun-ssh-icon-btn:hover { background: var(--dsw-alias-bg-layer-2, #1d212b); }
/* terminal */
.ckun-ssh-term-wrap { position: absolute; inset: 0; padding: 8px 10px; }
.ckun-ssh-term-wrap .xterm { height: 100%; background: transparent; }
.ckun-ssh-term-error {
  position: absolute; left: 12px; top: 8px; right: 12px; padding: 8px 10px;
  background: color-mix(in srgb, var(--dsw-alias-state-error-primary, #f85149) 15%, transparent);
  border: 1px solid var(--dsw-alias-state-error-primary, #f85149);
  border-radius: 6px; font-size: 12px;
}
/* tables / forms */
.ckun-ssh-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.ckun-ssh-table th {
  text-align: left; padding: 6px 10px; color: var(--dsw-alias-label-secondary, #9aa1b0);
  border-bottom: 1px solid var(--dsw-alias-border-l1, #262a33); font-weight: 500;
}
.ckun-ssh-table td { padding: 5px 10px; border-bottom: 1px solid var(--dsw-alias-border-l1, #262a33); }
.ckun-ssh-table tr:hover td { background: var(--dsw-alias-bg-layer-1, #171a21); }
.ckun-ssh-form-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.ckun-ssh-form-row label { width: 84px; flex: none; color: var(--dsw-alias-label-secondary, #9aa1b0); font-size: 12px; }
.ckun-ssh-input {
  flex: 1; padding: 5px 8px; font-size: 12px; min-width: 0;
  background: var(--dsw-alias-bg-layer-2, #1d212b); color: inherit;
  border: 1px solid var(--dsw-alias-border-l2, #333947); border-radius: 6px; outline: none;
}
.ckun-ssh-input:focus { border-color: var(--dsw-alias-brand-primary, #4c82f7); }
.ckun-ssh-select {
  padding: 5px 8px; font-size: 12px; background: var(--dsw-alias-bg-layer-2, #1d212b); color: inherit;
  border: 1px solid var(--dsw-alias-border-l2, #333947); border-radius: 6px;
}
/* modal */
.ckun-ssh-modal-backdrop {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0, 0, 0, .45); display: flex; align-items: center; justify-content: center;
}
.ckun-ssh-modal {
  width: 460px; max-height: 86vh; overflow-y: auto;
  background: var(--dsw-alias-bg-overlay, #1b1f29);
  border: 1px solid var(--dsw-alias-border-l2, #333947);
  border-radius: 10px; padding: 18px;
  box-shadow: 0 18px 60px rgba(0,0,0,.45);
}
.ckun-ssh-modal h3 { margin: 0 0 14px; font-size: 14px; }
.ckun-ssh-modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.ckun-ssh-fingerprint {
  font-family: ui-monospace, Consolas, monospace; font-size: 11px; word-break: break-all;
  background: var(--dsw-alias-bg-layer-2, #1d212b); border-radius: 6px; padding: 8px; margin: 8px 0;
}
.ckun-ssh-note { font-size: 11px; color: var(--dsw-alias-label-secondary, #9aa1b0); }
.ckun-ssh-error { color: var(--dsw-alias-state-error-primary, #f85149); font-size: 12px; margin-top: 8px; white-space: pre-wrap; }
.ckun-ssh-ok { color: var(--dsw-alias-state-success-primary, #3fb950); font-size: 12px; margin-top: 8px; }
.ckun-ssh-files-bar {
  display: flex; gap: 8px; padding: 10px 12px; align-items: center;
  border-bottom: 1px solid var(--dsw-alias-border-l1, #262a33);
}
.ckun-ssh-files-table { padding: 6px 12px 12px; overflow: auto; height: calc(100% - 52px); }
.ckun-ssh-mono { font-family: ui-monospace, Consolas, monospace; }
`
