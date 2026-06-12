try {
    const s1 = '{"k": "\\frac"}';
    console.log('1:', JSON.parse(s1));
} catch(e) {
    console.log('1 err:', e.message);
}

try {
    const s2 = '{"k": "\\cdot"}';
    console.log('2:', JSON.parse(s2));
} catch(e) {
    console.log('2 err:', e.message);
}

try {
    const s3 = '{"k": "\\\\frac"}';
    console.log('3:', JSON.parse(s3));
} catch(e) {
    console.log('3 err:', e.message);
}
