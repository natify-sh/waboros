// A GC workload for the waboros corpus: binary trees, a linked list, a hash map and string building.
// One exported function returns a checksum of everything.
@file:OptIn(kotlin.wasm.ExperimentalWasmInterop::class)

import kotlin.wasm.WasmExport

class Node(val left: Node?, val right: Node?) {
    fun check(): Int = 1 + (if (left == null) 0 else left.check() + right!!.check())
}

fun bottomUp(d: Int): Node = if (d == 0) Node(null, null) else Node(bottomUp(d - 1), bottomUp(d - 1))

class Cell(val v: Int, val next: Cell?)

fun work(): Long {
    var total = 0L
    val long = bottomUp(14)
    var d = 4
    while (d <= 14) {
        val iters = 1 shl (14 - d + 4)
        var c = 0L
        for (i in 0 until iters) c += bottomUp(d).check()
        total = total * 31 + c
        d += 2
    }
    total = total * 31 + long.check()
    var list: Cell? = null
    for (i in 0 until 200000) list = Cell(i * 7 % 1000, list)
    var s = 0L
    var p = list
    while (p != null) { s += p.v; p = p.next }
    total = total * 31 + s
    val m = HashMap<Int, Int>()
    for (i in 0 until 50000) {
        val k = (i.toLong() * 2654435761L % 10007).toInt()
        m[k] = (m[k] ?: 0) + i
    }
    var h = 0L
    for ((k, v) in m) h += k * 7L + v   // order independent
    total = total * 31 + h + m.size
    val sb = StringBuilder()
    for (i in 0 until 20000) { sb.append('k'); sb.append(i); sb.append(',') }
    val str = sb.toString()
    total = total * 31 + str.length + str.hashCode()
    return total
}

@WasmExport("run")
fun run(): Long = work()

fun main() {}
