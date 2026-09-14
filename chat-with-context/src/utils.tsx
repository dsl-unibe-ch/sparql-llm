export const queryLinkLabels = {
  label: "Run or edit the query",
  title: "Open the SPARQL query in an editor in a new tab",
};

export function getEditorUrl(query: string, endpointUrl: string = "") {
  return `https://sib-swiss.github.io/sparql-editor/?${endpointUrl ? `endpoint=${endpointUrl}&` : ""}query=${encodeURIComponent(query)}`;
}

const DAY_MS = 86_400_000;

/** Sort saved conversations (already newest first) into Today / Previous 7 days / Older. */
export function groupByRecency<T extends {updated_at: string}>(items: T[], now: Date = new Date()) {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const groups: {label: string; items: T[]}[] = [
    {label: "Today", items: []},
    {label: "Previous 7 days", items: []},
    {label: "Older", items: []},
  ];
  for (const item of items) {
    const time = new Date(item.updated_at).getTime();
    groups[time >= startOfToday ? 0 : time >= startOfToday - 7 * DAY_MS ? 1 : 2].items.push(item);
  }
  return groups.filter(group => group.items.length > 0);
}

// Get filters for color hex here: https://codepen.io/sosuke/pen/Pjoqqp
export const style = `chat-with-context {
  button:hover {
    filter: brightness(90%);
  }
}
.iconBtn {
  filter: invert(44%) sepia(22%) saturate(496%) hue-rotate(176deg) brightness(93%) contrast(79%);
}`;

// // A function to extract a SPARQL query from markdown text
// export function extractSparqlQuery(markdownContent: string) {
//   // Regular expression to match SPARQL queries within code blocks
//   const queryRegex = /```sparql([\s\S]*?)```/g;
//   const queries = [...markdownContent.matchAll(queryRegex)].map(match => match[1].trim());

//   // Get the last SPARQL query
//   const lastQuery = queries.length > 0 ? queries[queries.length - 1] : null;
//   if (!lastQuery) return null;

//   const endpointRegex = /#.*(https?:\/\/[^\s]+)/i;
//   const endpointMatch = lastQuery.match(endpointRegex);
//   const endpointUrl = endpointMatch ? endpointMatch[1] : null;
//   if (!endpointUrl) return null;
//   return getEditorUrl(lastQuery, endpointUrl);
// }
