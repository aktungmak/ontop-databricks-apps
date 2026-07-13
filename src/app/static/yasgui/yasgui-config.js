const yasgui = new Yasgui(document.getElementById("yasgui"), {
  requestConfig: {
    endpoint: "/sparql",
    method: "POST",
  },
  yasqe: {
    value: "SELECT * WHERE { ?s ?p ?o } LIMIT 10",
  },
});
