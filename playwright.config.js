const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
    testDir: './tests',
    timeout: 30000,
    webServer: {
        command: 'npx serve -l 3999 --no-clipboard',
        port: 3999,
        reuseExistingServer: true,
    },
    use: {
        baseURL: 'http://localhost:3999',
    },
    projects: [
        {
            name: 'webkit',
            use: {
                browserName: 'webkit',
                viewport: { width: 375, height: 812 },
                userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            },
        },
        {
            name: 'webkit-ipad',
            use: {
                browserName: 'webkit',
                viewport: { width: 820, height: 1180 },
                userAgent: 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            },
        },
    ],
});
