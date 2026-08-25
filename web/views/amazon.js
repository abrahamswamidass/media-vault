// Amazon view — stub. The agent already supports staging a file for Amazon's
// desktop app to pick up (`mediavault amazon upload`), but there's no
// multi-select UI here yet, and no agent-side process watching Firestore
// intents to act on a web-initiated request (same gap as "request full-res"
// in the Browse view — see the backlog issue for the intents processor).
export function mount(container) {
  container.innerHTML = `
    <div class="stub">
      <h2>Amazon staging</h2>
      <p>Coming soon. The idea: pick photos here and stage them for Amazon
      Photos' desktop app to auto-upload — the agent already has the
      mechanics (<code>mediavault amazon upload</code>), just not a way to
      trigger it from this page yet.</p>
      <p>Waiting on: an agent-side process that watches for requests from
      this page and acts on them.</p>
    </div>
  `;
}

export function unmount(container) {
  container.innerHTML = "";
}
