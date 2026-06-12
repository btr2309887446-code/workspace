const text = 'The velocity \\n is \\frac{1}{2} \\\\ \\n and \\alpha';
console.log("Original: " + text);
console.log("Replaced: " + text.replace(/\\(?!n)/g, '\\\\'));
