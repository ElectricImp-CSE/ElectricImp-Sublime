/**
 * Spec for AST Parser
 * @author Mikhail Yurasov <me@yurasov.me>
 */

'use strict';

require('jasmine-expect');

const fs = require('fs');
const path = require('path');
const AstParser = require('../../src/AstParser');

const FILE = __dirname + '/../fixtures/sample-1/input.nut';

describe('AstParser', () => {

  const parser = new AstParser();
  parser.file = path.basename(FILE);

  it('should do sample #1 alright', () => {
    const res = parser.parse(fs.readFileSync(FILE, 'utf-8'));
    expect(res).toEqual(require(FILE + '.json'));
    // console.log(JSON.stringify(res, null, '    '));
  });

});