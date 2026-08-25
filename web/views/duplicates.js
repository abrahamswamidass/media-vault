// Duplicates view — stub. Near-duplicate review is deliberately shelved
// (see GitHub issue #6): exact duplicates already auto-archive safely via
// `mediavault dedup`, and *near* duplicates (a resize, a re-compression) need
// a real side-by-side compare UI plus thumbnail dimensions/EXIF that aren't
// extracted yet. Nothing to wire up here until that groundwork lands.
export function mount(container) {
  container.innerHTML = `
    <div class="stub">
      <h2>Duplicate review</h2>
      <p>Coming soon. Exact duplicates on the NAS already get found and
      archived automatically by the agent (<code>mediavault dedup</code>) —
      this view will be for the harder case: near-duplicates (a resize, a
      re-compressed copy) that need a person to pick the better one.</p>
      <p>Waiting on: thumbnail dimensions/EXIF extraction, and a compare UI.</p>
    </div>
  `;
}

export function unmount(container) {
  container.innerHTML = "";
}
