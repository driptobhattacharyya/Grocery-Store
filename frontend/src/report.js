// CSV sales report download, shared by the manager and admin navbars.
//
// This is the one call that uses fetch (it needs the raw blob), so it adds
// the API base URL and the demo headers by hand and reports a lost session
// the same way the axios interceptors do.
import { apiUrl, sessionLost } from "./http";
import { getSandboxId, getToken } from "./auth";

/** Reads `filename` from a Content-Disposition header, if present. */
export function filenameFromDisposition(header, fallback = "order_report.csv") {
  if (!header) return fallback;
  const encoded = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* fall through to the plain form */
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : fallback;
}

export async function downloadReport() {
  try {
    const headers = { Authorization: `Bearer ${getToken()}` };
    if (getSandboxId()) headers["X-Sandbox-Id"] = getSandboxId();
    const response = await fetch(apiUrl("/api/manager/prepare_report"), { headers });
    if (!response.ok) {
      if (sessionLost(response.status, "/api/manager/prepare_report")) return;
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `Report download failed (HTTP ${response.status})`);
    }
    const filename = filenameFromDisposition(response.headers.get("Content-Disposition"));
    const url = window.URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
  } catch (error) {
    console.error("Error downloading order report:", error);
    alert(error instanceof TypeError ? "Could not reach the demo server for the report." : error.message);
  }
}
