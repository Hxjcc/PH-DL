import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse as parseSfc, compileScript } from '@vue/compiler-sfc'
import { parse as parseJs } from '@babel/parser'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SRC_DIR = path.resolve(__dirname, '../src')

const GLOBALS = new Set([
  'globalThis', 'window', 'document', 'console', 'undefined', 'NaN', 'Infinity', 'arguments',
  'Object', 'Array', 'String', 'Number', 'Boolean', 'BigInt', 'Symbol', 'Function', 'Math',
  'Date', 'RegExp', 'Error', 'TypeError', 'RangeError', 'SyntaxError', 'ReferenceError', 'URIError',
  'JSON', 'Intl', 'Map', 'Set', 'WeakMap', 'WeakSet', 'Promise', 'Proxy', 'Reflect',
  'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'encodeURIComponent', 'decodeURIComponent',
  'encodeURI', 'decodeURI', 'atob', 'btoa', 'structuredClone', 'queueMicrotask',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
  'requestAnimationFrame', 'cancelAnimationFrame', 'requestIdleCallback', 'cancelIdleCallback',
  'fetch', 'Headers', 'Request', 'Response', 'AbortController', 'AbortSignal',
  'URL', 'URLSearchParams', 'Blob', 'File', 'FileReader', 'FormData', 'Image', 'Audio',
  'WebSocket', 'Event', 'CustomEvent', 'MouseEvent', 'KeyboardEvent',
  'Element', 'HTMLElement', 'HTMLInputElement', 'HTMLMediaElement', 'HTMLVideoElement', 'Node',
  'ResizeObserver', 'IntersectionObserver', 'MutationObserver', 'DOMParser',
  'TextEncoder', 'TextDecoder', 'Uint8Array', 'ArrayBuffer',
  'localStorage', 'sessionStorage', 'navigator', 'location', 'history', 'performance', 'crypto',
  'getComputedStyle', 'matchMedia', 'CSS', 'MediaSource',
])

function listVueFiles(dir) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...listVueFiles(full))
    else if (entry.isFile() && entry.name.endsWith('.vue')) out.push(full)
  }
  return out
}

function collectPatternNames(pattern, targetSet) {
  if (!pattern) return
  switch (pattern.type) {
    case 'Identifier':
      targetSet.add(pattern.name)
      break
    case 'RestElement':
      collectPatternNames(pattern.argument, targetSet)
      break
    case 'AssignmentPattern':
      collectPatternNames(pattern.left, targetSet)
      break
    case 'ArrayPattern':
      for (const el of pattern.elements) collectPatternNames(el, targetSet)
      break
    case 'ObjectPattern':
      for (const prop of pattern.properties) {
        if (prop.type === 'RestElement') collectPatternNames(prop.argument, targetSet)
        else collectPatternNames(prop.value, targetSet)
      }
      break
  }
}

function hoistDeclarations(body, blockScope, fnScope) {
  for (const stmt of body) {
    if (!stmt) continue
    if (stmt.type === 'ImportDeclaration') {
      for (const spec of stmt.specifiers) {
        if (spec.local?.name) blockScope.add(spec.local.name)
      }
    } else if (stmt.type === 'FunctionDeclaration' && stmt.id?.name) {
      blockScope.add(stmt.id.name)
    } else if (stmt.type === 'ClassDeclaration' && stmt.id?.name) {
      blockScope.add(stmt.id.name)
    } else if (stmt.type === 'VariableDeclaration') {
      const target = stmt.kind === 'var' ? fnScope : blockScope
      for (const decl of stmt.declarations) collectPatternNames(decl.id, target)
    } else if (stmt.type === 'ExportNamedDeclaration' && stmt.declaration) {
      hoistDeclarations([stmt.declaration], blockScope, fnScope)
    } else if (stmt.type === 'ExportDefaultDeclaration' && stmt.declaration) {
      if ((stmt.declaration.type === 'FunctionDeclaration' || stmt.declaration.type === 'ClassDeclaration') && stmt.declaration.id?.name) {
        blockScope.add(stmt.declaration.id.name)
      }
    }
  }
}

function findUndeclaredIdentifiers(ast) {
  const undeclared = []
  const scopes = [new Set(GLOBALS)]

  function isDeclared(name) {
    for (let i = scopes.length - 1; i >= 0; i--) {
      if (scopes[i].has(name)) return true
    }
    return false
  }

  function visit(node, fnScope) {
    if (!node || typeof node !== 'object') return

    switch (node.type) {
      case 'Program': {
        const modScope = new Set()
        scopes.push(modScope)
        hoistDeclarations(node.body, modScope, modScope)
        for (const stmt of node.body) visit(stmt, modScope)
        scopes.pop()
        return
      }
      case 'BlockStatement': {
        const blockScope = new Set()
        scopes.push(blockScope)
        hoistDeclarations(node.body, blockScope, fnScope)
        for (const stmt of node.body) visit(stmt, fnScope)
        scopes.pop()
        return
      }
      case 'FunctionDeclaration':
      case 'FunctionExpression':
      case 'ArrowFunctionExpression': {
        const localScope = new Set()
        scopes.push(localScope)
        if (node.id?.name) localScope.add(node.id.name)
        for (const param of node.params) {
          collectPatternNames(param, localScope)
          visitPatternDefaults(param, localScope)
        }
        if (node.body.type === 'BlockStatement') {
          hoistDeclarations(node.body.body, localScope, localScope)
          for (const stmt of node.body.body) visit(stmt, localScope)
        } else {
          visit(node.body, localScope)
        }
        scopes.pop()
        return
      }
      case 'ObjectMethod':
      case 'ClassMethod':
      case 'ClassPrivateMethod': {
        if (node.computed) visit(node.key, fnScope)
        const localScope = new Set()
        scopes.push(localScope)
        for (const param of node.params) {
          collectPatternNames(param, localScope)
          visitPatternDefaults(param, localScope)
        }
        hoistDeclarations(node.body.body, localScope, localScope)
        for (const stmt of node.body.body) visit(stmt, localScope)
        scopes.pop()
        return
      }
      case 'CatchClause': {
        const catchScope = new Set()
        scopes.push(catchScope)
        if (node.param) collectPatternNames(node.param, catchScope)
        hoistDeclarations(node.body.body, catchScope, fnScope)
        for (const stmt of node.body.body) visit(stmt, fnScope)
        scopes.pop()
        return
      }
      case 'ForStatement':
      case 'ForInStatement':
      case 'ForOfStatement': {
        const loopScope = new Set()
        scopes.push(loopScope)
        const initNode = node.init || node.left
        if (initNode?.type === 'VariableDeclaration') {
          const target = initNode.kind === 'var' ? fnScope : loopScope
          for (const decl of initNode.declarations) collectPatternNames(decl.id, target)
          for (const decl of initNode.declarations) {
            visitPatternDefaults(decl.id, fnScope)
            if (decl.init) visit(decl.init, fnScope)
          }
        } else if (initNode) {
          visit(initNode, fnScope)
        }
        if (node.test) visit(node.test, fnScope)
        if (node.update) visit(node.update, fnScope)
        if (node.right) visit(node.right, fnScope)
        visit(node.body, fnScope)
        scopes.pop()
        return
      }
      case 'SwitchStatement': {
        visit(node.discriminant, fnScope)
        const swScope = new Set()
        scopes.push(swScope)
        for (const c of node.cases) hoistDeclarations(c.consequent, swScope, fnScope)
        for (const c of node.cases) {
          if (c.test) visit(c.test, fnScope)
          for (const stmt of c.consequent) visit(stmt, fnScope)
        }
        scopes.pop()
        return
      }
      case 'VariableDeclaration': {
        for (const decl of node.declarations) {
          visitPatternDefaults(decl.id, fnScope)
          if (decl.init) visit(decl.init, fnScope)
        }
        return
      }
      case 'ClassDeclaration':
      case 'ClassExpression': {
        const clsScope = new Set()
        scopes.push(clsScope)
        if (node.id?.name) clsScope.add(node.id.name)
        if (node.superClass) visit(node.superClass, fnScope)
        for (const item of node.body.body) visit(item, fnScope)
        scopes.pop()
        return
      }
      case 'ClassProperty':
      case 'ClassPrivateProperty': {
        if (node.computed) visit(node.key, fnScope)
        if (node.value) visit(node.value, fnScope)
        return
      }
      case 'MemberExpression':
      case 'OptionalMemberExpression': {
        visit(node.object, fnScope)
        if (node.computed) visit(node.property, fnScope)
        return
      }
      case 'ObjectProperty': {
        if (node.computed) visit(node.key, fnScope)
        visit(node.value, fnScope)
        return
      }
      case 'ImportDeclaration':
      case 'ExportAllDeclaration':
        return
      case 'ExportNamedDeclaration': {
        if (node.declaration) visit(node.declaration, fnScope)
        return
      }
      case 'ExportDefaultDeclaration': {
        visit(node.declaration, fnScope)
        return
      }
      case 'LabeledStatement': {
        visit(node.body, fnScope)
        return
      }
      case 'BreakStatement':
      case 'ContinueStatement':
      case 'MetaProperty':
        return
      case 'Identifier': {
        if (!isDeclared(node.name)) {
          undeclared.push({
            name: node.name,
            line: node.loc?.start?.line ?? 0,
            column: node.loc?.start?.column ?? 0,
          })
        }
        return
      }
      default: {
        for (const [key, val] of Object.entries(node)) {
          if (key === 'loc' || key === 'start' || key === 'end' || key === 'type') continue
          if (Array.isArray(val)) {
            for (const child of val) visit(child, fnScope)
          } else if (val && typeof val === 'object' && typeof val.type === 'string') {
            visit(val, fnScope)
          }
        }
      }
    }
  }

  function visitPatternDefaults(pattern, fnScope) {
    if (!pattern) return
    switch (pattern.type) {
      case 'AssignmentPattern':
        visitPatternDefaults(pattern.left, fnScope)
        visit(pattern.right, fnScope)
        break
      case 'ArrayPattern':
        for (const el of pattern.elements) visitPatternDefaults(el, fnScope)
        break
      case 'ObjectPattern':
        for (const prop of pattern.properties) {
          if (prop.type === 'RestElement') visitPatternDefaults(prop.argument, fnScope)
          else {
            if (prop.computed) visit(prop.key, fnScope)
            visitPatternDefaults(prop.value, fnScope)
          }
        }
        break
      case 'RestElement':
        visitPatternDefaults(pattern.argument, fnScope)
        break
    }
  }

  visit(ast.program, null)
  return undeclared
}

test('all Vue SFC scripts compile cleanly with zero undeclared identifier references', () => {
  const files = listVueFiles(SRC_DIR)
  assert.ok(files.length > 0, 'Expected Vue SFC files in frontend/src')

  const issues = []
  for (const file of files) {
    const rel = path.relative(SRC_DIR, file)
    const source = fs.readFileSync(file, 'utf8')
    const { descriptor, errors } = parseSfc(source, { filename: file })
    assert.equal(errors.length, 0, `${rel} failed SFC parse: ${errors.map((e) => e.message).join('; ')}`)
    if (!descriptor.script && !descriptor.scriptSetup) continue

    const compiled = compileScript(descriptor, { id: rel })
    const ast = parseJs(compiled.content, { sourceType: 'module' })
    const undefs = findUndeclaredIdentifiers(ast)
    for (const u of undefs) {
      issues.push(`${rel}: undeclared identifier '${u.name}' (compiled line ${u.line})`)
    }
  }

  assert.deepEqual(issues, [], `Found undeclared identifiers in Vue SFCs:\n${issues.join('\n')}`)
})
