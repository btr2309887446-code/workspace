const s = "A\nB\\frac";
console.log("Original:");
console.log(s);

const s2 = s.replace(/\\/g, "\\\\").replace(/\n/g, "\\n");
console.log("Processed:");
console.log(s2);
