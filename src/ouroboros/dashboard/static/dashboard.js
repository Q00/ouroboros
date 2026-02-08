/**
 * Ouroboros Neural Observatory - Dashboard Controller
 *
 * A sophisticated visualization system for AI iteration analysis
 * featuring real-time pattern detection, convergence tracking, and network visualization.
 */

// =============================================================================
// Configuration & State
// =============================================================================

const CONFIG = {
    phases: ['discover', 'define', 'develop', 'deliver'],
    phaseColors: {
        discover: '#a78bfa',
        define: '#60a5fa',
        develop: '#34d399',
        deliver: '#f87171'
    },
    outcomeColors: {
        success: '#2ed573',
        failure: '#ff4757',
        partial: '#ffb020',
        stagnant: '#9ba1b0',
        blocked: '#7b61ff'
    },
    severityColors: {
        critical: '#ff4757',
        high: '#ffb020',
        medium: '#7b61ff',
        low: '#5d6370'
    },
    animationDuration: 500,
    maxIterations: 200
};

let state = {
    iterations: [],
    patterns: [],
    network: { nodes: [], edges: [] },
    convergence: { values: [], rate: 0, isConverging: true },
    apiLogs: [],
    selectedIteration: null,
    iterationCount: 100
};

// =============================================================================
// DOM Elements
// =============================================================================

const elements = {
    iterationSlider: document.getElementById('iteration-slider'),
    iterationCount: document.getElementById('iteration-count'),
    analyzeBtn: document.getElementById('analyze-btn'),
    status: document.getElementById('status'),

    // Metrics
    totalIterations: document.getElementById('total-iterations'),
    tokenUsage: document.getElementById('token-usage'),
    tokenBar: document.getElementById('token-bar'),
    satisfaction: document.getElementById('satisfaction'),
    satisfactionRing: document.getElementById('satisfaction-ring'),
    patternsFound: document.getElementById('patterns-found'),
    patternBadges: document.getElementById('pattern-badges'),
    convergenceRate: document.getElementById('convergence-rate'),
    convergenceStatus: document.getElementById('convergence-status'),

    // Charts
    convergenceCanvas: document.getElementById('convergence-canvas'),
    chartAnnotations: document.getElementById('chart-annotations'),
    timelineItems: document.getElementById('timeline-items'),
    timelineMarkers: document.getElementById('timeline-markers'),
    networkCanvas: document.getElementById('network-canvas'),
    networkTooltip: document.getElementById('network-tooltip'),

    // Lists
    patternsList: document.getElementById('patterns-list'),
    challengeCard: document.getElementById('challenge-card'),
    socraticQuestions: document.getElementById('socratic-questions'),

    // API Log
    apiLog: document.getElementById('api-log'),
    apiCalls: document.getElementById('api-calls'),
    totalTokens: document.getElementById('total-tokens'),
    avgLatency: document.getElementById('avg-latency'),

    // Modal
    modal: document.getElementById('iteration-modal'),
    modalClose: document.getElementById('modal-close'),
    modalIterationId: document.getElementById('modal-iteration-id'),
    modalPhase: document.getElementById('modal-phase'),
    modalOutcome: document.getElementById('modal-outcome'),
    modalAction: document.getElementById('modal-action'),
    modalResult: document.getElementById('modal-result'),
    modalReasoning: document.getElementById('modal-reasoning'),
    modalState: document.getElementById('modal-state'),

    // Filters
    severityFilters: document.querySelectorAll('.filter-btn[data-severity]'),
    tabs: document.querySelectorAll('.tab[data-tab]')
};

// =============================================================================
// Data Generation (Simulated for Demo)
// =============================================================================

function generateIterations(count) {
    const iterations = [];
    let satisfaction = 0;
    let currentPhase = 0;
    const phaseThresholds = [0.25, 0.5, 0.75, 1.0];

    for (let i = 0; i < count; i++) {
        const progress = i / count;

        // Determine phase based on progress
        while (currentPhase < 3 && progress > phaseThresholds[currentPhase]) {
            currentPhase++;
        }

        // Simulate outcome with phase-dependent probabilities
        const rand = Math.random();
        let outcome;
        if (currentPhase === 0) {
            outcome = rand < 0.3 ? 'failure' : rand < 0.6 ? 'partial' : 'success';
        } else if (currentPhase === 1) {
            outcome = rand < 0.2 ? 'failure' : rand < 0.5 ? 'partial' : 'success';
        } else if (currentPhase === 2) {
            outcome = rand < 0.15 ? 'failure' : rand < 0.3 ? 'partial' : 'success';
        } else {
            outcome = rand < 0.1 ? 'failure' : rand < 0.2 ? 'partial' : 'success';
        }

        // Update satisfaction
        if (outcome === 'success') {
            satisfaction = Math.min(100, satisfaction + (5 + Math.random() * 5));
        } else if (outcome === 'partial') {
            satisfaction = Math.min(100, satisfaction + (1 + Math.random() * 2));
        } else {
            satisfaction = Math.max(0, satisfaction - (1 + Math.random() * 2));
        }

        iterations.push({
            id: i + 1,
            timestamp: new Date(Date.now() - (count - i) * 60000),
            phase: CONFIG.phases[currentPhase],
            outcome: outcome,
            action: generateAction(CONFIG.phases[currentPhase]),
            result: generateResult(outcome),
            reasoning: generateReasoning(CONFIG.phases[currentPhase], outcome),
            state: {
                position: [Math.floor(Math.random() * 20), Math.floor(Math.random() * 20)],
                items_collected: Math.floor(progress * 10),
                enemies_avoided: Math.floor(progress * 5),
                explored: Math.floor(progress * 100)
            },
            metrics: {
                efficiency: 0.3 + progress * 0.5 + (Math.random() * 0.2 - 0.1),
                coverage: progress * 0.8 + Math.random() * 0.2,
                pathLength: Math.floor(i * 2 + Math.random() * 10)
            },
            satisfaction: satisfaction,
            tokenCount: Math.floor(1000 + Math.random() * 4000)
        });
    }

    return iterations;
}

function generateAction(phase) {
    const actions = {
        discover: ['Exploring maze corridors', 'Scanning for item locations', 'Mapping unknown areas', 'Detecting enemy positions'],
        define: ['Analyzing shortest paths', 'Identifying critical routes', 'Evaluating item priorities', 'Assessing risk zones'],
        develop: ['Implementing pathfinding', 'Collecting target items', 'Avoiding enemy patrols', 'Optimizing movement'],
        deliver: ['Validating solution', 'Final path verification', 'Completing objectives', 'Exit sequence']
    };
    return actions[phase][Math.floor(Math.random() * actions[phase].length)];
}

function generateResult(outcome) {
    const results = {
        success: ['Objective completed', 'Target reached', 'Items secured', 'Path validated'],
        failure: ['Collision detected', 'Path blocked', 'Enemy encounter', 'Timeout exceeded'],
        partial: ['Partial progress', 'Intermediate goal reached', 'Alternative path found', 'Retry needed']
    };
    return results[outcome][Math.floor(Math.random() * results[outcome].length)];
}

function generateReasoning(phase, outcome) {
    if (outcome === 'success') {
        return `In the ${phase} phase, the agent successfully identified the optimal approach and executed it without issues.`;
    } else if (outcome === 'partial') {
        return `During ${phase}, progress was made but some obstacles required reconsideration of the strategy.`;
    }
    return `The ${phase} phase encountered challenges. The agent is analyzing alternative approaches.`;
}

function generatePatterns(iterations) {
    const patterns = [];

    // Detect spinning patterns
    let spinCount = 0;
    for (let i = 1; i < iterations.length; i++) {
        if (iterations[i].outcome === 'failure' && iterations[i-1].outcome === 'failure') {
            spinCount++;
        }
    }
    if (spinCount > 3) {
        patterns.push({
            id: 'spinning_001',
            category: 'spinning',
            severity: 'high',
            description: `Same error repeated ${spinCount} times consecutively`,
            occurrences: spinCount,
            confidence: Math.min(0.95, 0.5 + spinCount * 0.1),
            hypothesis: 'System is stuck in a loop, likely missing context',
            questions: [
                'Why is the same error occurring repeatedly?',
                'What context is the model missing?',
                'Would a different approach work better?'
            ]
        });
    }

    // Detect oscillation patterns
    let oscCount = 0;
    for (let i = 3; i < iterations.length; i++) {
        if (iterations[i].outcome === iterations[i-2].outcome &&
            iterations[i-1].outcome === iterations[i-3].outcome &&
            iterations[i].outcome !== iterations[i-1].outcome) {
            oscCount++;
        }
    }
    if (oscCount > 2) {
        patterns.push({
            id: 'oscillation_001',
            category: 'oscillation',
            severity: 'critical',
            description: 'Alternating between two error states',
            occurrences: oscCount * 4,
            confidence: 0.85,
            hypothesis: 'Fix attempts are contradictory, need different approach',
            questions: [
                'Why are fix attempts contradicting each other?',
                'What is the essential conflict between approaches?',
                'What would break this oscillation cycle?'
            ]
        });
    }

    // Detect stagnation
    let stagnantStreak = 0;
    let maxStagnant = 0;
    for (const iter of iterations) {
        if (iter.outcome === 'partial' || iter.outcome === 'failure') {
            stagnantStreak++;
            maxStagnant = Math.max(maxStagnant, stagnantStreak);
        } else {
            stagnantStreak = 0;
        }
    }
    if (maxStagnant > 5) {
        patterns.push({
            id: 'stagnation_001',
            category: 'stagnation',
            severity: 'medium',
            description: `No meaningful progress for ${maxStagnant} iterations`,
            occurrences: maxStagnant,
            confidence: 0.75,
            hypothesis: 'Task may require different approach or human guidance',
            questions: [
                'Why is no progress being made?',
                'Is the task properly scoped?',
                'Would human guidance help here?'
            ]
        });
    }

    // Add a dependency pattern for demo
    if (iterations.length > 50) {
        patterns.push({
            id: 'dependency_001',
            category: 'dependency',
            severity: 'high',
            description: 'Multiple iterations blocked by AC dependencies',
            occurrences: Math.floor(iterations.length * 0.1),
            confidence: 0.9,
            hypothesis: 'Task ordering issue - some ACs must complete first',
            questions: [
                'What must be completed first?',
                'Is there an implicit ordering in the requirements?',
                'Can the dependency be decoupled?'
            ]
        });
    }

    return patterns;
}

function generateNetwork(iterations, patterns) {
    const nodes = [];
    const edges = [];
    const nodeSet = new Set();

    // Add pattern nodes
    patterns.forEach((pattern, i) => {
        nodes.push({
            id: pattern.id,
            type: 'pattern',
            label: pattern.category,
            weight: pattern.occurrences,
            severity: pattern.severity,
            x: 0.3 + Math.random() * 0.4,
            y: 0.2 + (i / patterns.length) * 0.6
        });
        nodeSet.add(pattern.id);
    });

    // Add AC nodes based on phases
    CONFIG.phases.forEach((phase, i) => {
        const acId = `ac_${phase}`;
        nodes.push({
            id: acId,
            type: 'ac',
            label: phase.charAt(0).toUpperCase() + phase.slice(1),
            weight: iterations.filter(it => it.phase === phase).length,
            x: 0.1 + (i / CONFIG.phases.length) * 0.8,
            y: 0.8
        });
        nodeSet.add(acId);
    });

    // Add edges between patterns and phases
    patterns.forEach(pattern => {
        CONFIG.phases.forEach((phase, i) => {
            if (Math.random() > 0.5) {
                edges.push({
                    source: pattern.id,
                    target: `ac_${phase}`,
                    weight: Math.random() * pattern.occurrences,
                    type: 'affects'
                });
            }
        });
    });

    // Add inter-pattern edges
    for (let i = 0; i < patterns.length; i++) {
        for (let j = i + 1; j < patterns.length; j++) {
            if (Math.random() > 0.6) {
                edges.push({
                    source: patterns[i].id,
                    target: patterns[j].id,
                    weight: 1,
                    type: 'related'
                });
            }
        }
    }

    return { nodes, edges };
}

// =============================================================================
// Rendering Functions
// =============================================================================

function updateMetrics() {
    const iterations = state.iterations;
    if (!iterations.length) return;

    // Total iterations with animation
    animateValue(elements.totalIterations, 0, iterations.length, 800);

    // Token usage
    const totalTokens = iterations.reduce((sum, it) => sum + it.tokenCount, 0);
    const tokenPercent = Math.min(100, (totalTokens / 1000000) * 100);
    animateValue(elements.tokenUsage, 0, tokenPercent, 800, '%', 1);
    elements.tokenBar.style.width = `${tokenPercent}%`;

    // Satisfaction
    const currentSatisfaction = iterations[iterations.length - 1].satisfaction;
    animateValue(elements.satisfaction, 0, currentSatisfaction, 800, '%', 0);
    elements.satisfactionRing.setAttribute('stroke-dasharray', `${currentSatisfaction}, 100`);

    // Patterns
    animateValue(elements.patternsFound, 0, state.patterns.length, 500);
    renderPatternBadges();

    // Convergence rate
    const successCount = iterations.filter(it => it.outcome === 'success').length;
    const rate = (successCount / iterations.length) * 100;
    animateValue(elements.convergenceRate, 0, rate, 800, '%', 0);

    // Convergence status
    const isConverging = rate > 50;
    elements.convergenceStatus.className = `convergence-indicator ${isConverging ? 'converging' : 'stagnant'}`;
    elements.convergenceStatus.querySelector('.convergence-text').textContent = isConverging ? 'Converging' : 'Stagnant';
}

function animateValue(element, start, end, duration, suffix = '', decimals = 0) {
    const startTime = performance.now();
    const animate = (currentTime) => {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3); // Ease out cubic
        const current = start + (end - start) * eased;
        element.textContent = current.toFixed(decimals) + suffix;
        if (progress < 1) {
            requestAnimationFrame(animate);
        }
    };
    requestAnimationFrame(animate);
}

function renderPatternBadges() {
    const badges = state.patterns.map(p =>
        `<div class="pattern-badge ${p.severity}" title="${p.category}: ${p.severity}"></div>`
    ).join('');
    elements.patternBadges.innerHTML = badges;
}

function renderConvergenceChart() {
    const canvas = elements.convergenceCanvas;
    const ctx = canvas.getContext('2d');
    const rect = canvas.parentElement.getBoundingClientRect();

    // Set canvas size for retina
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    canvas.style.width = rect.width + 'px';
    canvas.style.height = rect.height + 'px';
    ctx.scale(dpr, dpr);

    const width = rect.width;
    const height = rect.height;
    const padding = { top: 20, right: 20, bottom: 30, left: 40 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;

    const iterations = state.iterations;
    if (!iterations.length) return;

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    // Draw grid
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
        const y = padding.top + (chartHeight / 4) * i;
        ctx.beginPath();
        ctx.moveTo(padding.left, y);
        ctx.lineTo(width - padding.right, y);
        ctx.stroke();
    }

    // Draw axes labels
    ctx.font = '10px JetBrains Mono';
    ctx.fillStyle = '#5d6370';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
        const y = padding.top + (chartHeight / 4) * i;
        ctx.fillText((100 - i * 25) + '%', padding.left - 8, y + 4);
    }

    // Calculate points
    const points = iterations.map((it, i) => ({
        x: padding.left + (i / (iterations.length - 1)) * chartWidth,
        y: padding.top + chartHeight - (it.satisfaction / 100) * chartHeight,
        phase: it.phase,
        outcome: it.outcome
    }));

    // Draw area gradient
    const gradient = ctx.createLinearGradient(0, padding.top, 0, padding.top + chartHeight);
    gradient.addColorStop(0, 'rgba(0, 240, 255, 0.3)');
    gradient.addColorStop(1, 'rgba(0, 240, 255, 0)');

    ctx.beginPath();
    ctx.moveTo(points[0].x, padding.top + chartHeight);
    points.forEach(p => ctx.lineTo(p.x, p.y));
    ctx.lineTo(points[points.length - 1].x, padding.top + chartHeight);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // Draw line
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
        const prev = points[i - 1];
        const curr = points[i];
        const cp1x = prev.x + (curr.x - prev.x) / 3;
        const cp2x = prev.x + 2 * (curr.x - prev.x) / 3;
        ctx.bezierCurveTo(cp1x, prev.y, cp2x, curr.y, curr.x, curr.y);
    }
    ctx.strokeStyle = '#00f0ff';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Draw phase transition markers
    let lastPhase = points[0].phase;
    elements.chartAnnotations.innerHTML = '';

    points.forEach((p, i) => {
        if (p.phase !== lastPhase) {
            // Draw vertical line
            ctx.beginPath();
            ctx.moveTo(p.x, padding.top);
            ctx.lineTo(p.x, padding.top + chartHeight);
            ctx.strokeStyle = CONFIG.phaseColors[p.phase];
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 4]);
            ctx.stroke();
            ctx.setLineDash([]);

            // Add annotation
            const annotation = document.createElement('div');
            annotation.className = 'chart-annotation';
            annotation.style.left = p.x + 'px';
            annotation.style.top = '5px';
            annotation.style.background = CONFIG.phaseColors[p.phase];
            annotation.style.color = '#0a0b0f';
            annotation.textContent = p.phase.charAt(0).toUpperCase() + p.phase.slice(1);
            elements.chartAnnotations.appendChild(annotation);

            lastPhase = p.phase;
        }
    });

    // Draw dots for key points
    points.forEach((p, i) => {
        if (i % Math.ceil(points.length / 20) === 0) {
            ctx.beginPath();
            ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
            ctx.fillStyle = CONFIG.outcomeColors[p.outcome];
            ctx.fill();
        }
    });
}

function renderTimeline() {
    const iterations = state.iterations;
    if (!iterations.length) return;

    // Render items
    const maxMetric = Math.max(...iterations.map(it => it.metrics.efficiency));
    const items = iterations.map((it, i) => {
        const height = 10 + (it.metrics.efficiency / maxMetric) * 40;
        return `<div class="timeline-item ${it.phase} ${it.outcome}"
                     style="height: ${height}px"
                     data-index="${i}"
                     title="Iteration ${it.id}: ${it.phase} - ${it.outcome}"></div>`;
    }).join('');
    elements.timelineItems.innerHTML = items;

    // Render markers
    const markerCount = Math.min(10, iterations.length);
    const step = Math.floor(iterations.length / markerCount);
    const markers = [];
    for (let i = 0; i < iterations.length; i += step) {
        markers.push(`<span class="timeline-marker">${iterations[i].id}</span>`);
    }
    elements.timelineMarkers.innerHTML = markers.join('');

    // Add click handlers
    elements.timelineItems.querySelectorAll('.timeline-item').forEach(item => {
        item.addEventListener('click', () => {
            const index = parseInt(item.dataset.index);
            openIterationModal(iterations[index]);
        });
    });
}

function renderNetwork() {
    const canvas = elements.networkCanvas;
    const ctx = canvas.getContext('2d');
    const container = canvas.parentElement;
    const rect = container.getBoundingClientRect();

    // Set canvas size
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    canvas.style.width = rect.width + 'px';
    canvas.style.height = rect.height + 'px';
    ctx.scale(dpr, dpr);

    const width = rect.width;
    const height = rect.height;
    const { nodes, edges } = state.network;

    if (!nodes.length) {
        ctx.fillStyle = '#5d6370';
        ctx.font = '14px Outfit';
        ctx.textAlign = 'center';
        ctx.fillText('Run analysis to generate pattern network', width / 2, height / 2);
        return;
    }

    // Clear
    ctx.clearRect(0, 0, width, height);

    // Draw edges
    edges.forEach(edge => {
        const source = nodes.find(n => n.id === edge.source);
        const target = nodes.find(n => n.id === edge.target);
        if (!source || !target) return;

        const sx = source.x * width;
        const sy = source.y * height;
        const tx = target.x * width;
        const ty = target.y * height;

        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(tx, ty);
        ctx.strokeStyle = edge.type === 'affects' ? 'rgba(0, 240, 255, 0.2)' : 'rgba(123, 97, 255, 0.2)';
        ctx.lineWidth = Math.max(1, edge.weight * 0.5);
        ctx.stroke();
    });

    // Draw nodes
    nodes.forEach(node => {
        const x = node.x * width;
        const y = node.y * height;
        const radius = node.type === 'pattern' ? 8 + Math.min(node.weight, 20) * 0.5 : 12;

        // Glow
        const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius * 2);
        if (node.type === 'pattern') {
            const color = CONFIG.severityColors[node.severity] || '#00f0ff';
            gradient.addColorStop(0, color + '40');
            gradient.addColorStop(1, 'transparent');
        } else {
            gradient.addColorStop(0, '#7b61ff40');
            gradient.addColorStop(1, 'transparent');
        }
        ctx.beginPath();
        ctx.arc(x, y, radius * 2, 0, Math.PI * 2);
        ctx.fillStyle = gradient;
        ctx.fill();

        // Node
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        if (node.type === 'pattern') {
            ctx.fillStyle = CONFIG.severityColors[node.severity] || '#00f0ff';
        } else {
            ctx.fillStyle = '#7b61ff';
        }
        ctx.fill();

        // Label
        ctx.fillStyle = '#f0f2f5';
        ctx.font = '11px Outfit';
        ctx.textAlign = 'center';
        ctx.fillText(node.label, x, y + radius + 16);
    });

    // Add hover interaction
    canvas.onmousemove = (e) => {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        let hoveredNode = null;
        for (const node of nodes) {
            const x = node.x * width;
            const y = node.y * height;
            const dist = Math.sqrt((mx - x) ** 2 + (my - y) ** 2);
            if (dist < 20) {
                hoveredNode = node;
                break;
            }
        }

        if (hoveredNode) {
            elements.networkTooltip.innerHTML = `
                <strong>${hoveredNode.label}</strong><br>
                Type: ${hoveredNode.type}<br>
                Weight: ${hoveredNode.weight}
                ${hoveredNode.severity ? `<br>Severity: ${hoveredNode.severity}` : ''}
            `;
            elements.networkTooltip.style.left = (mx + 10) + 'px';
            elements.networkTooltip.style.top = (my - 10) + 'px';
            elements.networkTooltip.classList.add('visible');
            canvas.style.cursor = 'pointer';
        } else {
            elements.networkTooltip.classList.remove('visible');
            canvas.style.cursor = 'default';
        }
    };

    canvas.onmouseleave = () => {
        elements.networkTooltip.classList.remove('visible');
    };
}

function renderPatterns(filter = 'all') {
    const patterns = filter === 'all'
        ? state.patterns
        : state.patterns.filter(p => p.severity === filter);

    if (!patterns.length) {
        elements.patternsList.innerHTML = `
            <div class="pattern-item" style="text-align: center; color: var(--text-tertiary);">
                No patterns detected yet
            </div>
        `;
        return;
    }

    const html = patterns.map(p => `
        <div class="pattern-item ${p.severity}" data-id="${p.id}">
            <div class="pattern-header">
                <span class="pattern-category ${p.category}">${p.category}</span>
                <span class="pattern-count">${p.occurrences}x</span>
            </div>
            <p class="pattern-description">${p.description}</p>
            <div class="pattern-confidence">
                <span>Confidence</span>
                <div class="confidence-bar">
                    <div class="confidence-fill" style="width: ${p.confidence * 100}%"></div>
                </div>
                <span>${Math.round(p.confidence * 100)}%</span>
            </div>
        </div>
    `).join('');

    elements.patternsList.innerHTML = html;

    // Add click handlers to show questions
    elements.patternsList.querySelectorAll('.pattern-item').forEach(item => {
        item.addEventListener('click', () => {
            const pattern = state.patterns.find(p => p.id === item.dataset.id);
            if (pattern) showPatternQuestions(pattern);
        });
    });
}

function showPatternQuestions(pattern) {
    // Update challenge card
    elements.challengeCard.innerHTML = `
        <div class="challenge-icon">⚡</div>
        <div class="challenge-content">
            <p class="challenge-text"><strong>${pattern.category.toUpperCase()}</strong>: ${pattern.hypothesis}</p>
        </div>
    `;

    // Update Socratic questions
    const questionsHtml = pattern.questions.map(q => `
        <div class="socratic-question">
            <span class="question-icon">?</span>
            <span>${q}</span>
        </div>
    `).join('');

    elements.socraticQuestions.innerHTML = questionsHtml;

    // Update intensity
    const intensity = pattern.severity === 'critical' ? 'critical' :
                      pattern.severity === 'high' ? 'intense' : 'moderate';
    document.getElementById('intensity').className = `intensity-indicator ${intensity}`;
    document.querySelector('.intensity-text').textContent = intensity.charAt(0).toUpperCase() + intensity.slice(1);
}

function addApiLogEntry(type, tokens, latency, status = 'success') {
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-US', { hour12: false });

    const entry = {
        type,
        tokens,
        latency,
        status,
        time: timeStr
    };

    state.apiLogs.unshift(entry);
    if (state.apiLogs.length > 20) state.apiLogs.pop();

    renderApiLog();
    updateApiStats();
}

function renderApiLog() {
    if (!state.apiLogs.length) {
        elements.apiLog.innerHTML = `
            <div class="log-entry placeholder">
                <span class="log-icon">◌</span>
                <span class="log-text">Waiting for API activity...</span>
            </div>
        `;
        return;
    }

    const html = state.apiLogs.map(log => `
        <div class="log-entry ${log.status}">
            <span class="log-icon">${log.status === 'success' ? '●' : log.status === 'error' ? '✕' : '◐'}</span>
            <span class="log-type">${log.type}</span>
            <span class="log-tokens">${formatNumber(log.tokens)} tok</span>
            <span class="log-latency">${log.latency}ms</span>
            <span class="log-time">${log.time}</span>
        </div>
    `).join('');

    elements.apiLog.innerHTML = html;
}

function updateApiStats() {
    const logs = state.apiLogs.filter(l => l.status === 'success');
    const calls = logs.length;
    const tokens = logs.reduce((sum, l) => sum + l.tokens, 0);
    const avgLatency = logs.length ? Math.round(logs.reduce((sum, l) => sum + l.latency, 0) / logs.length) : 0;

    elements.apiCalls.textContent = calls;
    elements.totalTokens.textContent = formatNumber(tokens);
    elements.avgLatency.textContent = avgLatency;
}

function formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toString();
}

function openIterationModal(iteration) {
    elements.modalIterationId.textContent = '#' + iteration.id;
    elements.modalPhase.textContent = iteration.phase;
    elements.modalPhase.className = `phase-badge ${iteration.phase}`;
    elements.modalOutcome.textContent = iteration.outcome;
    elements.modalOutcome.className = `outcome-badge ${iteration.outcome}`;
    elements.modalAction.textContent = iteration.action;
    elements.modalResult.textContent = iteration.result;
    elements.modalReasoning.textContent = iteration.reasoning;
    elements.modalState.textContent = JSON.stringify(iteration.state, null, 2);

    elements.modal.classList.add('open');
    state.selectedIteration = iteration;
}

function closeModal() {
    elements.modal.classList.remove('open');
    state.selectedIteration = null;
}

// =============================================================================
// Main Analysis Function
// =============================================================================

async function runAnalysis() {
    const btn = elements.analyzeBtn;
    const status = elements.status;

    // Update UI state
    btn.classList.add('loading');
    btn.disabled = true;
    status.className = 'status-indicator analyzing';
    status.querySelector('.status-text').textContent = 'Analyzing';

    try {
        // Simulate API call delay
        await sleep(500);
        addApiLogEntry('generate_iterations', state.iterationCount * 50, 120);

        // Generate data
        state.iterations = generateIterations(state.iterationCount);
        updateMetrics();

        await sleep(300);
        addApiLogEntry('analyze_patterns', state.iterations.length * 100, 450);

        // Generate patterns
        state.patterns = generatePatterns(state.iterations);
        renderPatterns();

        await sleep(200);
        addApiLogEntry('build_network', state.patterns.length * 200, 280);

        // Generate network
        state.network = generateNetwork(state.iterations, state.patterns);

        // Render all visualizations
        renderConvergenceChart();
        renderTimeline();
        renderNetwork();

        // Show first pattern questions if any
        if (state.patterns.length > 0) {
            showPatternQuestions(state.patterns[0]);
        }

        addApiLogEntry('full_history_analysis', state.iterations.reduce((s, i) => s + i.tokenCount, 0), 1250);

        // Update status
        status.className = 'status-indicator';
        status.querySelector('.status-text').textContent = 'Complete';

    } catch (error) {
        console.error('Analysis failed:', error);
        status.className = 'status-indicator error';
        status.querySelector('.status-text').textContent = 'Error';
        addApiLogEntry('error', 0, 0, 'error');
    } finally {
        btn.classList.remove('loading');
        btn.disabled = false;
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// =============================================================================
// Event Listeners
// =============================================================================

function initEventListeners() {
    // Iteration slider
    elements.iterationSlider.addEventListener('input', (e) => {
        state.iterationCount = parseInt(e.target.value);
        elements.iterationCount.textContent = state.iterationCount;
    });

    // Analyze button
    elements.analyzeBtn.addEventListener('click', runAnalysis);

    // Modal close
    elements.modalClose.addEventListener('click', closeModal);
    document.querySelector('.modal-backdrop').addEventListener('click', closeModal);

    // Severity filters
    elements.severityFilters.forEach(btn => {
        btn.addEventListener('click', () => {
            elements.severityFilters.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderPatterns(btn.dataset.severity);
        });
    });

    // Tabs
    elements.tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const parent = tab.parentElement;
            parent.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            // Tab content switching would go here
        });
    });

    // Window resize
    let resizeTimeout;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimeout);
        resizeTimeout = setTimeout(() => {
            if (state.iterations.length) {
                renderConvergenceChart();
                renderNetwork();
            }
        }, 250);
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
        if (e.key === 'Enter' && e.ctrlKey) runAnalysis();
    });
}

// =============================================================================
// Initialize
// =============================================================================

function init() {
    initEventListeners();
    renderApiLog();

    // Initial empty chart
    renderConvergenceChart();
    renderNetwork();

    console.log('🐍 Ouroboros Neural Observatory initialized');
    console.log('Press Ctrl+Enter or click "Run Analysis" to begin');
}

// Start when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
