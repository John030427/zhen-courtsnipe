/**
 * Idempotent Clash profile transform: route only reservation.sustech.edu.cn to local MITM.
 * Compose with an existing script: const config = applySustechMitmExtension(baseConfig);
 */
function applySustechMitmExtension(config, options = {}) {
  const mitmName = options.proxyName || "SUSTech-MITM";
  const server = options.server || "127.0.0.1";
  const port = options.port || 8080;
  const host = options.domain || "reservation.sustech.edu.cn";
  if (options.processPaths !== undefined && !Array.isArray(options.processPaths)) {
    throw new Error("processPaths must be an array of absolute paths");
  }
  const processPaths = [...new Set(options.processPaths || [])];
  for (const path of processPaths) {
    if (typeof path !== "string" || !/^(?:[A-Za-z]:\\|\/)/.test(path) || /[,()\r\n]/.test(path)) {
      throw new Error("processPaths must contain absolute paths without rule delimiters");
    }
  }
  const next = JSON.parse(JSON.stringify(config || {}));
  next.proxies = Array.isArray(next.proxies) ? next.proxies.slice() : [];
  next.rules = Array.isArray(next.rules) ? next.rules.slice() : [];

  const proxy = { name: mitmName, type: "http", server, port };
  const withoutDup = next.proxies.filter((p) => p && p.name !== mitmName);
  withoutDup.push(proxy);
  next.proxies = withoutDup;

  const rule = `DOMAIN,${host},${mitmName}`;
  const withoutRule = next.rules.filter((r) => r !== rule);
  const bypass = processPaths.map((path) => `AND,((DOMAIN,${host}),(PROCESS-PATH,${path})),DIRECT`);
  next.rules = [...bypass, rule, ...withoutRule.filter((r) => !bypass.includes(r))];
  if (processPaths.length) {
    const dns = next.dns || {};
    if ((dns["fake-ip-filter-mode"] || "blacklist") !== "blacklist") {
      throw new Error("Compose non-blacklist DNS filters manually before enabling TUN capture");
    }
    if (dns["fake-ip-filter"] && !Array.isArray(dns["fake-ip-filter"])) {
      throw new Error("fake-ip-filter must be an array");
    }
    next.dns = { ...dns, "fake-ip-filter": [...new Set([...(dns["fake-ip-filter"] || []), host])] };
    if (next["find-process-mode"] === "off") next["find-process-mode"] = "strict";
  }
  return next;
}

module.exports = { applySustechMitmExtension };
