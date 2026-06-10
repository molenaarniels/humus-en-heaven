// shared.js — gedeelde Gist/token/workflow-logica voor de schrijvende
// dashboards (index, mowing, airflow). Vóór het pagina-script laden.
//
// Het token staat alleen in déze browser (localStorage) en hoort een
// fine-grained token te zijn dat tot de ene Gist is gescopet. Pagina's
// zonder schrijf-functie (window, accuracy, model, ipad) laden dit niet.

const CONFIG = { gistId: "__GIST_ID__", githubToken: null };

{
  const lsGist = localStorage.getItem("gist_id");
  const lsToken = localStorage.getItem("gh_token");
  if (lsGist) CONFIG.gistId = lsGist;
  if (lsToken) CONFIG.githubToken = lsToken;
}

// Eerste keer: vraag Gist ID + token en bewaar ze. Geeft false bij annuleren.
function ensureGistConfig() {
  if (CONFIG.gistId && CONFIG.gistId !== "__GIST_ID__" && CONFIG.githubToken) return true;
  const gid = prompt("Eerste keer: plak je Gist ID (uit de URL van je gist):");
  if (!gid) return false;
  const cleanGid = gid.trim();
  if (!/^[a-f0-9]{20,}$/i.test(cleanGid)) {
    alert("Dit ziet er niet uit als een Gist ID (verwacht een hex-string).");
    return false;
  }
  const token = prompt(
    "Plak een GitHub fine-grained token met 'Gists: read & write' " +
    "(scope dit token tot alleen deze gist).\n\n" +
    "Let op: het token wordt in jouw browser opgeslagen — gebruik geen " +
    "classic PAT met brede 'gist' scope, want die geeft toegang tot al je gists."
  );
  if (!token) return false;
  const cleanToken = token.trim();
  // github_pat_ = fine-grained, ghp_ = classic. Waarschuw bij classic.
  if (cleanToken.startsWith("ghp_")) {
    const ok = confirm(
      "Dit lijkt een classic PAT (ghp_…). Die geeft toegang tot ÁL jouw gists, " +
      "niet alleen deze. Liever een fine-grained token (github_pat_…) maken.\n\n" +
      "Toch doorgaan?"
    );
    if (!ok) return false;
  }
  localStorage.setItem("gist_id", cleanGid);
  localStorage.setItem("gh_token", cleanToken);
  CONFIG.gistId = cleanGid;
  CONFIG.githubToken = cleanToken;
  return true;
}

function forgetCredentials() {
  if (!confirm("Gist ID en token uit deze browser verwijderen?")) return;
  localStorage.removeItem("gist_id");
  localStorage.removeItem("gh_token");
  CONFIG.gistId = "__GIST_ID__";
  CONFIG.githubToken = null;
  alert("Verwijderd. Je krijgt opnieuw de prompt zodra je iets opslaat.");
}

// Content van één bestand uit de Gist (string), of fallback als het ontbreekt.
async function gistReadFileContent(filename, fallback = null) {
  const r = await fetch(`https://api.github.com/gists/${CONFIG.gistId}`,
    { headers: { Authorization: `token ${CONFIG.githubToken}` } });
  if (!r.ok) throw new Error(`Gist fetch: HTTP ${r.status}`);
  const j = await r.json();
  return j.files?.[filename]?.content ?? fallback;
}

async function gistWriteFile(filename, content) {
  const r = await fetch(`https://api.github.com/gists/${CONFIG.gistId}`, {
    method: "PATCH",
    headers: { Authorization: `token ${CONFIG.githubToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ files: { [filename]: { content } } }),
  });
  if (!r.ok) throw new Error(`Gist save: HTTP ${r.status}`);
}

// Start de bijbehorende GitHub Action (alleen op *.github.io met repo-pad).
async function dispatchWorkflow(workflowFile) {
  const host = location.host;
  const path = location.pathname.split('/').filter(Boolean);
  if (!host.endsWith('.github.io') || path.length === 0) return;
  const user = host.split('.')[0];
  const repo = path[0];
  try {
    await fetch(`https://api.github.com/repos/${user}/${repo}/actions/workflows/${workflowFile}/dispatches`, {
      method: "POST",
      headers: { Authorization: `token ${CONFIG.githubToken}`, "Content-Type": "application/json", Accept: "application/vnd.github+json" },
      body: JSON.stringify({ ref: "main" }),
    });
  } catch (e) { console.warn("Workflow trigger failed:", e); }
}
