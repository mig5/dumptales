#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *next_row(PyObject *self, PyObject *args) {
    PyObject *sql;
    Py_ssize_t at;
    if (!PyArg_ParseTuple(args, "On", &sql, &at)) return NULL;
    if (!PyUnicode_Check(sql)) {
        PyErr_SetString(PyExc_TypeError, "SQL statement must be Unicode");
        return NULL;
    }
    if (PyUnicode_READY(sql) < 0) return NULL;
    Py_ssize_t length = PyUnicode_GET_LENGTH(sql);
    int kind = PyUnicode_KIND(sql);
    void *data = PyUnicode_DATA(sql);
    if (at < 0 || at >= length || PyUnicode_READ(kind, data, at) != '(') {
        PyErr_SetString(PyExc_ValueError, "expected opening parenthesis");
        return NULL;
    }
    PyObject *values = PyList_New(0);
    if (!values) return NULL;
    Py_ssize_t start = at + 1;
    Py_UCS4 quote = 0;
    int depth = 1;
    for (Py_ssize_t i = start; i < length; i++) {
        Py_UCS4 ch = PyUnicode_READ(kind, data, i);
        if (quote) {
            if (ch == '\\') {
                if (i + 1 < length) i++;
                continue;
            }
            if (ch == quote) {
                if (i + 1 < length && PyUnicode_READ(kind, data, i + 1) == quote) {
                    i++;
                    continue;
                }
                quote = 0;
            }
            continue;
        }
        if (ch == '\'' || ch == '"' || ch == '`') {
            quote = ch;
        } else if (ch == '(') {
            depth++;
        } else if (ch == ')') {
            depth--;
            if (depth == 0) {
                Py_ssize_t left = start, right = i;
                while (left < right && Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, left))) left++;
                while (right > left && Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, right - 1))) right--;
                PyObject *token = PyUnicode_Substring(sql, left, right);
                if (!token || PyList_Append(values, token) < 0) { Py_XDECREF(token); Py_DECREF(values); return NULL; }
                Py_DECREF(token);
                PyObject *result = PyTuple_New(2);
                if (!result) { Py_DECREF(values); return NULL; }
                PyObject *position = PyLong_FromSsize_t(i + 1);
                if (!position) { Py_DECREF(result); Py_DECREF(values); return NULL; }
                PyTuple_SET_ITEM(result, 0, values);
                PyTuple_SET_ITEM(result, 1, position);
                return result;
            }
        } else if (ch == ',' && depth == 1) {
            Py_ssize_t left = start, right = i;
            while (left < right && Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, left))) left++;
            while (right > left && Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, right - 1))) right--;
            PyObject *token = PyUnicode_Substring(sql, left, right);
            if (!token || PyList_Append(values, token) < 0) { Py_XDECREF(token); Py_DECREF(values); return NULL; }
            Py_DECREF(token);
            start = i + 1;
        }
    }
    Py_DECREF(values);
    PyErr_SetString(PyExc_ValueError, "unbalanced parentheses");
    return NULL;
}

static PyMethodDef methods[] = {
    {"next_row", next_row, METH_VARARGS, "Parse the next SQL VALUES tuple without copying the remaining statement."},
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_dumptales_native", NULL, -1, methods};
PyMODINIT_FUNC PyInit__dumptales_native(void) { return PyModule_Create(&module); }
