/* C++ exceptions: an expression evaluator over the lines of io[0..n). Each line gives one int32 at
   io[OUT+4*i]: its value, or -1000-position for a ParseError, -2000 for DivZero, -3000 for another
   exception (Other, caught by catch (...)). A parse error is caught in eval_line, counted and
   rethrown (throw;), then turned into a number three frames up. RAII objects check that the
   destructors run during unwinding: the last int32 is the number of destructors that ran. */
#include "common.h"
extern "C" void heap_reset(void);
struct ParseError { int pos; };
struct DivZero {};
struct Other { int x; };
static int dtors, rethrown;
struct Guard { ~Guard() { dtors++; } };
struct P {
  const uint8_t *s; int n, i;
  int peek() { return i < n ? s[i] : 0; }
  int number() {
    Guard g;
    if (peek() < '0' || peek() > '9') throw ParseError{i};
    int v = 0;
    while (peek() >= '0' && peek() <= '9') v = v * 10 + (s[i++] - '0');
    return v;
  }
  int atom() {
    Guard g;
    if (peek() == '(') {
      i++;
      int v = expr();
      if (peek() != ')') throw ParseError{i};
      i++;
      return v;
    }
    if (peek() == '!') throw Other{i};
    return number();
  }
  int term() {
    Guard g;
    int v = atom();
    for (;;) {
      int c = peek();
      if (c != '*' && c != '/') return v;
      i++;
      int r = atom();
      if (c == '*') v *= r;
      else { if (r == 0) throw DivZero(); v /= r; }
    }
  }
  int expr() {
    Guard g;
    int v = term();
    for (;;) {
      int c = peek();
      if (c != '+' && c != '-') return v;
      i++;
      int r = term();
      v = c == '+' ? v + r : v - r;
    }
  }
};
static int eval_line(const uint8_t *s, int n) {
  P p{s, n, 0};
  try {
    int v = p.expr();
    if (p.i != n) throw ParseError{p.i};
    return v;
  } catch (ParseError &e) {
    rethrown++;
    throw;
  }
}
static int safe(const uint8_t *s, int n) {
  try { return eval_line(s, n); }
  catch (ParseError &e) { return -1000 - e.pos; }
  catch (DivZero &) { return -2000; }
  catch (...) { return -3000; }
}
extern "C" EXPORT(exc_eval) int exc_eval(int n, int reps) {
  int k = 0;
  while (reps-- > 0) {
    heap_reset(); dtors = rethrown = k = 0;
    for (int a = 0, i = 0; i <= n; i++)
      if (i == n || io[i] == '\n') {
        int v = safe(io + a, i - a);
        for (int j = 0; j < 4; j++) io[OUT + 4 * k + j] = (uint32_t)v >> (8 * j);
        k++; a = i + 1;
      }
    for (int j = 0; j < 4; j++) io[OUT + 4 * k + j] = (uint32_t)dtors >> (8 * j);
    for (int j = 0; j < 4; j++) io[OUT + 4 * k + 4 + j] = (uint32_t)rethrown >> (8 * j);
  }
  return 4 * k + 8;
}
