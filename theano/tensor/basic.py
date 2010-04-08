"""A `Type` and `Op` classes to work with numpy.ndarrays symbolically."""

__docformat__ = "restructuredtext en"

import __builtin__
import sys # for sys.maxint
from theano.configparser import config, AddConfigVar, BoolParam
import traceback #for overriding Op.__call__
if sys.version_info >= (2,5):
  import functools

import numpy, theano
from copy import copy

from theano import gof
from theano.gof import Variable, Op, utils, Type, Constant,  Value
from theano.tensor.tsor_apply import Apply

from theano import gradient

import elemwise
from theano import scalar as scal
from theano.gof.python25 import partial, any, all

from theano import compile, printing
from theano.printing import pprint, Print

### set up the external interface
from elemwise import Elemwise, DimShuffle, CAReduce, Sum

import logging
_logger=logging.getLogger("theano.tensor.basic")
def _info(*msg):
    _logger.info(' '.join(msg))
def _warn(*msg):
    _logger.warn(' '.join(msg))

def check_equal_numpy(x, y):
    """
    Returns True iff x and y are equal (checks the dtype and
    shape if x and y are numpy.ndarray instances).
    """
    if isinstance(x, numpy.ndarray) and isinstance(y, numpy.ndarray):
        return x.dtype == y.dtype and x.shape == y.shape and numpy.any(abs(x - y) < 1e-10)
    elif isinstance(x, numpy.random.RandomState) and isinstance(y, numpy.random.RandomState):
        return all(numpy.all(a==b) for a, b in zip(x.__getstate__(), y.__getstate__()))
    else:
        return x == y

compile.register_checker(check_equal_numpy)

def hashtype(self):
    t = type(self)
    return hash(t.__name__) ^ hash(t.__module__)
elemwise.hashtype = hashtype


__oplist_constructor_list = []
"""List of functions to be listed as op constructors in the oplist (`gen_oplist`, doc/oplist.txt)."""
def constructor(f):
    """Add `f` to :doc:`oplist`.
    
    Make `f` appear as a constructor in the oplist (`gen_oplist`, doc/oplist.txt).
    """
    __oplist_constructor_list.append(f)
    return f
def __oplist_tag(thing, tag):
    tags = getattr(thing, '__oplist_tags', [])
    tags.append(tag)
    thing.__oplist_tags = tags


if 0:
    # this starts to feel like we're enumerating all the types
    # the one place where this is used we should also allow for sparse
    # variables
    # - JB 20100226
    def as_cuda_or_tensor_variable(x, name = None, ndim=None):
        """
        This function do the same as_tensor_variable, but don't transfert the value on the gpu
        """
        if hasattr(x, '_as_CudaNdarrayVariable'):
            return x._as_CudaNdarrayVariable() #TODO: pass name and ndim arguments
        return as_tensor_variable(x, name, ndim)
    
def as_tensor_variable(x, name = None, ndim=None):
    """Return `x`, transformed into a `TensorType`

    This function is often used by `make_node` methods of `Op` subclasses to
    turn ndarrays, numbers, `Scalar` instances, `Apply` instances and `TensorType`
    instances into valid input list elemnts.

    :Parameters:
     - `x`: Apply instance, Variable instance, numpy.ndarray, or number
       This thing will be transformed into a `Variable` in a sensible way.  An
       ndarray argument will not be copied, but a list of numbers will be copied
       to make an ndarray.
     - `name`: str or None
       If a new `Variable` instance is created, it will be named with this string.
     - `ndim`: None or integer
       Return a Variable with this many dimensions.  Raise TypeError if it's not possible.

    :Exceptions:
     - `ValueError`: raised if an `Apply` with no default output is fetched
     - `TypeError`: raised if `x` cannot be converted to a TensorType Variable

    """
    if hasattr(x, '_as_TensorVariable'):
        return x._as_TensorVariable() #TODO: pass name and ndim arguments

    if isinstance(x, gof.Apply):
        #TODO: use Apply's default output mechanism
        if len(x.outputs) != 1:
            raise ValueError("It is ambiguous which output of a multi-output Op has to be fetched.", x)
        else:
            x = x.outputs[0]
    if isinstance(x, Variable):
        if isinstance(x.type, scal.Scalar):
            x = tensor_from_scalar(x)

        if not isinstance(x.type, TensorType):
            raise TypeError("Variable type field must be a TensorType.", x, x.type)

        if ndim is None:
            return x
        else:
            if (x.type.ndim > ndim):
                #TODO: strip off leading broadcastable dimensions
                raise ValueError('TensorType could not be cast to have %i dimensions' % ndim, x.type)
            elif (x.type.ndim < ndim):
                return shape_padleft(x, n_ones=(ndim - x.type.ndim))
            else:
                return x
    if isinstance(x, (tuple, list)) and any(isinstance(xi, Variable) for xi in x):
        try:
            return stack(*x)
        except (TypeError, ValueError):
            pass

    try:
        return constant(x, name=name, ndim=ndim)
    except TypeError:
        try:
            str_x = str(x)
        except:
            str_x = repr(x)
        raise TypeError("Cannot convert %s to TensorType" % str_x, type(x))

# this has a different name, because _as_tensor_variable is the function which ops use
# to upcast their arguments... this internal-use function is a good place to put debugging stuff, better than the global astensor.
_as_tensor_variable = as_tensor_variable

as_tensor = as_tensor_variable

class NumpyAutocaster(object):
    """ This class is used to cast python ints and floats to numpy arrays.
    
    Python ints are always 64bit and floats are always double precision.
    This class uses the algorithm in __call__ to use a narrower dtype when no precision would
    be lost, and to even lose precision when this is demanded (e.g. to automatically cast all
    floats to single-precision).

    """
    def __init__(self, dtypes):
        self.dtypes = tuple(dtypes)
    def __call__(self, x):
        # change the default casting behaviour for python floats to always cast to float32
        if config.floatX == 'float32' and config.floatX in self.dtypes:
            return theano._asarray(x, dtype='float32')
        for dtype in self.dtypes:
            x_ = theano._asarray(x, dtype=dtype)
            if numpy.all(x == x_):
                break
        # returns either an exact x_==x, or the last casted x_
        return x_
autocast_int = NumpyAutocaster(('int8', 'int16', 'int32', 'int64'))
autocast_float = NumpyAutocaster(('float32', 'float64'))
# autocast_float dtypes might be manipulated in tensor.__init__
#
# Note: it's a bit weird for a compiler to automatically downcast literals like this, and it might
# have implications for efficiency when mixing types.  For example when you add 1.0 +
# dmatrix(), the 1.0 could be converted to float32, and require upcasting for the + operation
# at every position in the dmatrix.  using theano._asarray(1.0, dtype='float64') will circumvent
# this autocasting, and in future, our ops might be smarter about factoring out upcasts.   The
# advantage of this mechanism is to combine it with floatX so that 1.0 + xmatrix() will always
# have the same type as the xmatrix().
# 
class autocast_float_as(object):
    """This class makes it possible to temporarily and locally adjust autocasting behaviour.

    For example:
    >>> with autocast_float_as('float32') as _dummy:
    >>>    assert (fvector() + 1.1).dtype == 'float32'  # temporary downcasting
    >>> assert (fvector() + 1.1).dtype == 'float64'     # back to default behaviour

    This class might be convenient in some code, but it definitely helps to test the
    autocasting mechanism.
    """
    def __init__(self, *dtypes):
        self.dtypes = dtypes
    def __enter__(self):
        self.old_dtypes = autocast_float.dtypes
        autocast_float.dtypes = self.dtypes
    def __exit__(self, *args):
        autocast_float.dtypes = self.old_dtypes

def constant_or_value(x, rtype, name=None, ndim=None, dtype=None):
    """Return a symbolic `Constant` with value `x`
    
    :Exceptions:
     - `TypeError`: `x` could not be converted to a numpy.ndarray
     - `ValueError`: `x` could not be expanded to have ndim dimensions

    """
    if dtype is not None:
        # in this case, the semantics are that the caller is forcing the dtype
        x_ = theano._asarray(x, dtype=dtype)
    else:
        # in this case, this function should infer the dtype according to the autocasting
        # rules.  See autocasting above.
        x_ = None
        if rtype is TensorConstant and isinstance(x, int):
            x_ = autocast_int(x)
        elif rtype is TensorConstant and isinstance(x, float):
            x_ = autocast_float(x)
        elif isinstance(x, numpy.ndarray):
            x_ = x
        else:
            x_ = numpy.asarray(x)

    assert type(x_) == numpy.ndarray

    bcastable = [d == 1 for d in x_.shape]
    if ndim is not None:
        if len(bcastable) < ndim:
            bcastable = [True] * (ndim - len(bcastable)) + bcastable
        elif len(bcastable) > ndim:
            #TODO: strip off dimensions of size 1
            raise ValueError('ndarray could not be cast to constant with %i dimensions' % ndim)
        assert len(bcastable) == ndim

    try:
        if rtype is TensorConstant:
            rval = rtype(
                    TensorType(dtype = x_.dtype, broadcastable = bcastable),
                    x_.copy(),
                    name=name)
            rval.tag.shape = x_.shape
            return rval
        else:
            # leave the shape out of the type
            return rtype(TensorType(dtype = x_.dtype, broadcastable = bcastable), x_, name=name)
    except:
        raise TypeError("Could not convert %s to TensorType" % x, type(x))

def constant(x, name=None, ndim=None, dtype=None):
    return constant_or_value(x, rtype=TensorConstant, name=name, ndim=ndim, dtype=dtype)

def value(x, name=None, ndim=None, dtype=None):
    return constant_or_value(x, rtype=TensorValue, name=name, ndim=ndim, dtype=dtype)

def _obj_is_wrappable_as_tensor(x):
    try:
        constant(x)
        return True
    except TypeError:
        return False
def _wrap_tensor_into_member(x):
    return compile.module.Member(constant(x))
compile.module.register_wrapper(_obj_is_wrappable_as_tensor, _wrap_tensor_into_member)

if int(config.tensor.cmp_sloppy)>1:
    # This config variable is a quick-and-dirty way to get low-precision
    # comparisons.  For a more precise setting of these tolerances set
    # them explicitly in your user code by assigning, for example,
    # "theano.tensor.basic.float32_atol = ..."

    # When config.tensor.cmp_sloppy>1 we are even more sloppy. This is
    # useful to test the GPU as they don't use extended precision and
    # this cause some difference bigger then the normal sloppy.
    float32_atol = 5e-4
    float32_rtol = 1e-3 
    float64_rtol = 1e-4
    float64_atol = 1e-3
elif int(config.tensor.cmp_sloppy):
    float32_atol = 1e-4
    float32_rtol = 1e-3 
    float64_rtol = 1e-4
    float64_atol = 1e-3
else:
    #If you change those value in test don't forget to put them back when the test end.
    #Don't forget the case when the test fail.
    float32_atol = 1e-5
    float32_rtol = 1e-3 

    # defaults in numpy.allclose
    float64_rtol = 1.0000000000000001e-05
    float64_atol = 1e-8

def _allclose(a, b):
    narrow = 'float32', 'complex64'
    if (str(a.dtype) in narrow) or (str(b.dtype) in narrow):
        atol = float32_atol
        rtol = float32_rtol
    else:
        atol = float64_atol
        rtol = float64_rtol
    return numpy.allclose(a,b, atol=atol, rtol=rtol)

def get_constant_value(v):
    """return the constant scalar(0-D) value underlying variable `v`

    If v is the output of dimshuffles, fills, allocs, rebroadcasts,
    this function digs through them.

    If `v` is not some view of constant data, then raise a TypeError.

    :note: There may be another function similar to this one in the code, but I'm not sure where it
    is.
    """

    if isinstance(v, Constant):
        #TODO: consider checking for arrays of the form e.g. [1,1,1,1] where
        # it is not a constant, but in some cases it *could* be replaced with one.
        # Note that this would have an effect on the broadcasting of inputs and so on
        try:
            complex(v.data) #works for all numeric scalars
            return v.data
        except:
            raise TypeError(v)
    if v.owner:
        if isinstance(v.owner.op, Alloc):
            return get_constant_value(v.owner.inputs[0])
        if isinstance(v.owner.op, DimShuffle):
            return get_constant_value(v.owner.inputs[0])
        if isinstance(v.owner.op, Rebroadcast):
            return get_constant_value(v.owner.inputs[0])
        if v.owner.op == fill:
            shape, val = v.owner.inputs
            # fill(a,b) fills the shape of 'a' filled with 'b'
            return get_constant_value(val)
    raise TypeError(v)


class TensorType(Type):
    """Symbolic `Type` representing a numpy.ndarray value."""

    filter_checks_isfinite = False
    """
    When this is True, strict filtering rejects data containing NaN or Inf entries. (Used in `DebugMode`)
    """

    def __init__(self, dtype, broadcastable, name = None):
        """Initialize self.dtype and self.broadcastable.

        :Parameters:
         - `dtype`: str corresponding to numpy dtype (e.g., 'int64')
           The value (ndarray) associated to a `Variable` of this `Type` will have
           this dtype.
         - `broadcastable`: tuple, list, or array of boolean values
           This argument serves two purposes.  First, the True elements of this
           list indicate the dimensions where the shape of an associated value
           must be 1.  Secondly, the length of this list is the number of
           dimensions that an associated value must have.  See
           :doc:`broadcasting` for an explanation of how this list is used.
         - `name`: str
           Optional name for this type.
        """
        self.dtype = str(dtype)
        if self.dtype=='floatX':
          self.dtype=config.floatX
        ###    broadcastable is immutable, and all elements are either True or False
        self.broadcastable = tuple(bool(b) for b in broadcastable) 
        self.dtype_specs() # error checking is done there
        self.name = name
        self.numpy_dtype = numpy.dtype(self.dtype)

    def filter(self, data, strict = False):
        """Convert `data` to something which can be associated to a `TensorVariable`.

        This function is not meant to be called in user code.  It is for
        `Linker` instances to use when running a compiled graph.
        """
        if (type(data) is numpy.ndarray) and (data.dtype is self.numpy_dtype):
            pass # fall through to ndim check
        elif strict:
            # this is its own subcase that doesn't fall through to anything
            if not isinstance(data, numpy.ndarray):
                raise TypeError("%s expected a ndarray object.", data, type(data))
            if not str(data.dtype) == self.dtype:
                raise TypeError("%s expected a ndarray object with dtype = %s (got %s)." % (self, self.dtype, data.dtype))
            if not data.ndim == self.ndim:
                raise TypeError("%s expected a ndarray object with %s dimensions (got %s)." % (self, self.ndim, data.ndim))

            return data
        else:
            data = theano._asarray(data, dtype = self.dtype) #TODO - consider to pad shape with ones
            # to make it consistent with self.broadcastable... like vector->row type thing
        if self.ndim != data.ndim:
            raise TypeError("Wrong number of dimensions: expected %s, got %s with shape %s." % (self.ndim, data.ndim, data.shape), data)
        i = 0
        for b in self.broadcastable:
            if b and data.shape[i] != 1:
                raise TypeError("Non-unit value on shape on a broadcastable dimension.", data.shape, self.broadcastable)
            i+=1
        if self.filter_checks_isfinite and (not numpy.all(numpy.isfinite(data))):
            raise ValueError("non-finite elements not allowed")
        return data

    def dtype_specs(self):
        """Return a tuple (python type, c type, numpy typenum) that corresponds to
        self.dtype.
        
        This function is used internally as part of C code generation.
        """
        #TODO: add more type correspondances for e.g. int32, int64, float32,
        #complex64, etc.
        try:
            return {'float32': (float, 'npy_float32', 'NPY_FLOAT32'),
                    'float64': (float, 'npy_float64', 'NPY_FLOAT64'),
                    'uint8': (int, 'npy_uint8', 'NPY_UINT8'),
                    'int8': (int, 'npy_int8', 'NPY_INT8'),
                    'uint16': (int, 'npy_uint16', 'NPY_UINT16'),
                    'int16': (int, 'npy_int16', 'NPY_INT16'),
                    'uint32': (int, 'npy_uint32', 'NPY_UINT32'),
                    'int32': (int, 'npy_int32', 'NPY_INT32'),
                    'uint64': (int, 'npy_uint64', 'NPY_UINT64'),
                    'int64': (int, 'npy_int64', 'NPY_INT64'),
                    'complex128': (complex, 'theano_complex128', 'NPY_COMPLEX128'),
                    'complex64': (complex, 'theano_complex64', 'NPY_COMPLEX64')}[self.dtype]
        except KeyError:
            raise TypeError("Unsupported dtype for %s: %s" % (self.__class__.__name__, self.dtype))

    def to_scalar_type(self):
        return scal.Scalar(dtype = self.dtype)

    def __eq__(self, other):
        """Compare True iff other is the same kind of TensorType"""
        return type(self) == type(other) and other.dtype == self.dtype \
            and other.broadcastable == self.broadcastable

    @staticmethod
    def values_eq(a, b):
        #TODO: check to see if the dtype and shapes must match
        #      for now, we err on safe side...
        if a.shape != b.shape:
            return False
        if a.dtype != b.dtype:
            return False
        a_eq_b = (a==b)
        r = numpy.all(a_eq_b)
        if r: return True
        # maybe the trouble is that there are NaNs 
        a_missing = numpy.isnan(a)
        if a_missing.any():
            b_missing = numpy.isnan(b)
            return numpy.all(a_eq_b + (a_missing == b_missing))
        else:
            return False
    @staticmethod
    def values_eq_approx(a, b):
        if type(a) is numpy.ndarray and type(b) is numpy.ndarray:
            if a.shape != b.shape:
                return False
            if a.dtype != b.dtype:
                return False
            if 'int' in str(a.dtype):
                return numpy.all(a==b)
            elif a.shape == (): #for comparing scalars, use broadcasting.
                # Note: according to James B, there was a reason for the
                # following two lines, that may seem weird at first glance.
                # If someone can figure out what it is, please say it here!
                ones = numpy.ones(2)
                return _allclose(ones * a, ones*b)
            else:
                cmp = _allclose(a, b)
                if cmp:
                    # Numpy claims they are close, this is good enough for us.
                    return True
                # Numpy is unhappy, but it does not necessarily mean that a and
                # b are different. Indeed, Numpy does not like missing values
                # and will return False whenever some are found in a or b.
                # The proper way would be to use the MaskArray stuff available
                # in Numpy. However, it looks like it has been added to Numpy's
                # core recently, so it may not be available to everyone. Thus,
                # for now we use a home-made recipe, that should probably be
                # revisited in the future.
                a_missing = numpy.isnan(a)
                if not a_missing.any():
                    # There are no missing values in a, thus this is not the
                    # reason why numpy.allclose(a, b) returned False.
                    _info('numpy allclose failed for abs_err %f and rel_err %f' %(
                        numpy.max( abs(a-b)),
                        numpy.max( abs(a-b)/(abs(a)+abs(b)))))
                    return False
                # The following line is what numpy.allclose bases its decision
                # upon, according to its documentation.
                rtol = 1.0000000000000001e-05
                atol = 1e-8
                cmp_elemwise = (numpy.absolute(a - b) <=
                        (atol + rtol * numpy.absolute(b)))
                # Find places where both a and b have missing values.
                both_missing = a_missing * numpy.isnan(b)
                # Combine all information.
                return (cmp_elemwise + both_missing).all()

        return False

    def __hash__(self):
        """Hash equal for same kinds of TensorType"""
        return hashtype(self) ^ hash(self.dtype) ^ hash(self.broadcastable)

    ndim = property(lambda self: len(self.broadcastable), doc = "number of dimensions")
    """Number of dimensions

    This read-only property is the preferred way to get the number of dimensions
    of a `TensorType`.

    """

    def make_variable(self, name = None):
        """Return a `TensorVariable` of this type

        :Parameters:
         - `name`: str
           A pretty name to identify this `Variable` when printing and debugging

        """
        return TensorVariable(self, name = name)

    def __str__(self):
        if self.name:
            return self.name
        else:
            b = self.broadcastable
            named_broadcastable = {(): 'scalar',
                     (False,): 'vector',
                     (False, True): 'col',
                     (True, False): 'row',
                     (False, False): 'matrix'}
            if b in named_broadcastable:
                bcast = named_broadcastable[b]
            else:
                if any(b):
                    bcast = str(b)
                else:
                        bcast = '%iD' % len(b)
            return "TensorType(%s, %s)" % (str(self.dtype), bcast)

    def __repr__(self):
        return str(self)
        #"TensorType{%s, %s}" % (str(self.dtype), str(self.broadcastable))

    def c_declare(self, name, sub):
        """Override `CLinkerOp.c_declare` """
        return """
        PyArrayObject* %(name)s;
        int type_num_%(name)s;
        typedef %(dtype)s dtype_%(name)s;
        """ % dict(sub, name = name, dtype = self.dtype_specs()[1])

    def c_init(self, name, sub):
        """Override `CLinkerOp.c_init` """
        return """
        %(name)s = NULL;
        type_num_%(name)s = %(type_num)s;
        """ % dict(sub, name = name, type_num = self.dtype_specs()[2])

    def c_extract(self, name, sub):
        """Override `CLinkerOp.c_extract` """
        # TODO: make the error message print out the dtype of the
        # input received.
        return """
        %(name)s = NULL;
        if (py_%(name)s == Py_None) {
            // We can either fail here or set %(name)s to NULL and rely on Ops using
            // tensors to handle the NULL case, but if they fail to do so they'll end up
            // with nasty segfaults, so this is public service.
            PyErr_SetString(PyExc_ValueError, "expected an ndarray, not None");
            %(fail)s
        }
        if (!PyArray_Check(py_%(name)s)) {
            PyErr_SetString(PyExc_ValueError, "expected an ndarray");
            %(fail)s
        }
        type_num_%(name)s = ((PyArrayObject*)py_%(name)s)->descr->type_num; //we expect %(type_num)s
        if (type_num_%(name)s != %(type_num)s) {
            PyErr_SetString(PyExc_ValueError, "expected %(type_num)s");
            %(fail)s
        }
        %(name)s = (PyArrayObject*)(py_%(name)s);
        Py_XINCREF(%(name)s);
        """ % dict(sub, name = name, type_num = self.dtype_specs()[2])

    def c_cleanup(self, name, sub):
        """Override `CLinkerOp.c_cleanup` """
        return """
        if (%(name)s) {
            Py_XDECREF(%(name)s);
        }
        """ % locals()

    def c_sync(self, name, sub):
        """Override `CLinkerOp.c_sync` """
        return """
        {Py_XDECREF(py_%(name)s);}
        if (!%(name)s) {
            Py_INCREF(Py_None);
            py_%(name)s = Py_None;
        }
        else if ((void*)py_%(name)s != (void*)%(name)s) {
            py_%(name)s = (PyObject*)%(name)s;
        }
        {Py_XINCREF(py_%(name)s);}
        """ % locals()

    def c_headers(self):
        """Override `CLinkerOp.c_headers` """
        return scal.Scalar(self.dtype).c_headers()

    def c_libraries(self):
        return scal.Scalar(self.dtype).c_libraries()

    def c_compile_args(self):
        return scal.Scalar(self.dtype).c_compile_args()

    def c_support_code(self):
        """Override `CLinkerOp.c_support_code` """
        return scal.Scalar(self.dtype).c_support_code()

    def c_code_cache_version(self):
        scalar_version = scal.Scalar(self.dtype).c_code_cache_version()
        if scalar_version:
            return (2,) + scalar_version
        else:
            return ()

# Easy constructors

def tensor(*args, **kwargs):
    name = kwargs.get('name',None)
    return TensorType(*args, **kwargs).make_variable(name=name)

def _multi(*fns):
    def f2(f, *names):
        if names and isinstance(names[0], int):
            if names == 1:
                return f()
            else:
                return [f() for i in xrange(names[0])]
        if isinstance(names, tuple):
            if len(names) == 1:
                names = names[0]
        if len(names) == 1:
            return f(names)
        else:
            return [f(name) for name in names]
    if len(fns) == 1:
        return partial(f2, fns)
    else:
        return [partial(f2, f) for f in fns]

cscalar = TensorType('complex64', ())
zscalar = TensorType('complex128', ())
fscalar = TensorType('float32', ())
dscalar = TensorType('float64', ())
bscalar = TensorType('int8', ())
wscalar = TensorType('int16', ())
iscalar = TensorType('int32', ())
lscalar = TensorType('int64', ())
def scalar(name = None, dtype = None):
    """Return a symbolic scalar variable.
    :param dtype: numeric type (None means to use theano.config.floatX)
    :param name: a name to attach to this variable
    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, ())
    return type(name)
scalars, fscalars, dscalars, iscalars, lscalars = _multi(scalar, fscalar, dscalar, iscalar, lscalar)

int_types = bscalar, wscalar, iscalar, lscalar
float_types = fscalar, dscalar
complex_types = cscalar, zscalar
int_scalar_types = int_types
float_scalar_types = float_types
complex_scalar_types = complex_types

cvector = TensorType('complex64', (False, ))
zvector = TensorType('complex128', (False, ))
fvector = TensorType('float32', (False, ))
dvector = TensorType('float64', (False, ))
bvector = TensorType('int8', (False,))
wvector = TensorType('int16', (False,))
ivector = TensorType('int32', (False, ))
lvector = TensorType('int64', (False, ))
def vector(name = None, dtype = None):
    """Return a symbolic vector variable.
    :param dtype: numeric type (None means to use theano.config.floatX)
    :param name: a name to attach to this variable
    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, (False, ))
    return type(name)
vectors, fvectors, dvectors, ivectors, lvectors = _multi(vector, fvector, dvector, ivector, lvector)

int_vector_types = bvector, wvector, ivector, lvector
float_vector_types = fvector, dvector
complex_vector_types = cvector, zvector

cmatrix = TensorType('complex64', (False, False))
zmatrix = TensorType('complex128', (False, False))
fmatrix = TensorType('float32', (False, False))
dmatrix = TensorType('float64', (False, False))
bmatrix = TensorType('int8', (False, False))
wmatrix = TensorType('int16', (False, False))
imatrix = TensorType('int32', (False, False))
lmatrix = TensorType('int64', (False, False))
def matrix(name = None, dtype = None):
    """Return a symbolic matrix variable.
    :param dtype: numeric type (None means to use theano.config.floatX)
    :param name: a name to attach to this variable
    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, (False, False))
    return type(name)
matrices, fmatrices, dmatrices, imatrices, lmatrices = _multi(matrix, fmatrix, dmatrix, imatrix, lmatrix)

int_matrix_types = bmatrix, wmatrix, imatrix, lmatrix
float_matrix_types = fmatrix, dmatrix
complex_matrix_types = cmatrix, zmatrix

crow = TensorType('complex64', (True, False))
zrow = TensorType('complex128', (True, False))
frow = TensorType('float32', (True, False))
drow = TensorType('float64', (True, False))
brow = TensorType('int8', (True, False))
wrow = TensorType('int16', (True, False))
irow = TensorType('int32', (True, False))
lrow = TensorType('int64', (True, False))
def row(name = None, dtype = None):
    """Return a symbolic row variable (ndim=2, broadcastable=[True,False]).
    :param dtype: numeric type (None means to use theano.config.floatX)
    :param name: a name to attach to this variable
    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, (True, False))
    return type(name)
rows, frows, drows, irows, lrows = _multi(row, frow, drow, irow, lrow)

ccol = TensorType('complex64', (False, True))
zcol = TensorType('complex128', (False, True))
fcol = TensorType('float32', (False, True))
dcol = TensorType('float64', (False, True))
bcol = TensorType('int8', (False, True))
wcol = TensorType('int16', (False, True))
icol = TensorType('int32', (False, True))
lcol = TensorType('int64', (False, True))
def col(name = None, dtype = None):
    """Return a symbolic column variable (ndim=2, broadcastable=[False,True]).
    :param dtype: numeric type (None means to use theano.config.floatX)
    :param name: a name to attach to this variable
    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, (False, True))
    return type(name)
cols, fcols, dcols, icols, lcols = _multi(col, fcol, dcol, icol, lcol)

ctensor3 = TensorType('complex64', (False,)*3)
ztensor3 = TensorType('complex128', (False,)*3)
ftensor3 = TensorType('float32', (False,)*3)
dtensor3 = TensorType('float64', (False,)*3)
btensor3 = TensorType('int8', (False,)*3)
wtensor3 = TensorType('int16', (False,)*3)
itensor3 = TensorType('int32', (False,)*3)
ltensor3 = TensorType('int64', (False,)*3)
def tensor3(name=None, dtype=None):
    """Return a symbolic 3-D variable.
    :param dtype: numeric type (None means to use theano.config.floatX)
    :param name: a name to attach to this variable
    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, (False, False, False))
    return type(name)
tensor3s, ftensor3s, dtensor3s, itensor3s, ltensor3s = _multi(tensor3, ftensor3, dtensor3,
        itensor3, ltensor3)

ctensor4 = TensorType('complex64', (False,)*4)
ztensor4 = TensorType('complex128', (False,)*4)
ftensor4 = TensorType('float32', (False,)*4)
dtensor4 = TensorType('float64', (False,)*4)
btensor4 = TensorType('int8', (False,)*4)
wtensor4 = TensorType('int16', (False,)*4)
itensor4 = TensorType('int32', (False,)*4)
ltensor4 = TensorType('int64', (False,)*4)
def tensor4(name=None, dtype=None):
    """Return a symbolic 4-D variable.
    :param dtype: numeric type (None means to use theano.config.floatX)
    :param name: a name to attach to this variable
    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, (False, False, False, False))
    return type(name)
tensor4s, ftensor4s, dtensor4s, itensor4s, ltensor4s = _multi(tensor4, ftensor4, dtensor4,
        itensor4, ltensor4)

class _tensor_py_operators:
    #UNARY
    def __abs__(self): return abs_(self)
    def __neg__(self): return neg(self)

    #CASTS 
    #### REMOVED THESE BECAUSE PYTHON appears to require __int__ to return an int. -JB 20081112
    #def __int__(self): return convert_to_int32(self)
    #def __float__(self): return convert_to_float64(self)
    #def __complex__(self): return convert_to_complex128(self)

    #COMPARISONS
    def __lt__(self,other): return lt(self, other)
    def __le__(self,other): return le(self, other)
    def __gt__(self,other): return gt(self, other)
    def __ge__(self,other): return ge(self, other)

    #BITWISE
    def __invert__(self): return invert(self) 
    def __and__(self,other): return and_(self, other)
    def __or__(self,other): return or_(self, other)
    def __xor__(self,other): return xor(self, other)
    def __rand__(self,other): return and_(other,self)
    def __ror__(self,other): return or_(other, self)
    def __rxor__(self,other): return xor(other, self)
#     def __iand__(self, other): return _and_inplace(self, other)
#     def __ior__(self, other): return _or_inplace(self, other)
#     def __ixor__(self, other): return _xor_inplace(self, other)

    #ARITHMETIC - NORMAL
    def __add__(self,other): 
        try:
            return add(self,other)
        except Exception, e:
            return NotImplemented
    def __sub__(self,other): 
        try:
            return sub(self,other)
        except Exception, e:
            return NotImplemented
    def __mul__(self,other): 
        try: 
            return mul(self,other)
        except Exception, e:
            return NotImplemented
    def __div__(self,other): 
        try: 
            return div_proxy(self,other)
        except Exception, e:
            return NotImplemented
    def __pow__(self,other): 
        try:
            return pow(self,other)
        except Exception, e:
            return NotImplemented
    def __mod__(self,other):
        try:
            return mod(self,other)
        except Exception, e:
            return NotImplemented

#     ##### DON"T USE THESE BECAUSE INPLACE OPS SHOULD BE INSERTED BY OPTIMIZATION ONLY
#     #ARITHMETIC - INPLACE
#     def __iadd__(self,other): return _add_inplace(self,other)
#     def __isub__(self,other): return _sub_inplace(self,other)
#     def __imul__(self,other): return _mul_inplace(self,other)
#     def __idiv__(self,other): return _div_inplace(self,other)
#     def __ipow__(self,other): return _pow_inplace(self,other)

    #ARITHMETIC - RIGHT-OPERAND
    def __radd__(self,other): return add(other,self)
    def __rsub__(self,other): return sub(other,self)
    def __rmul__(self,other): return mul(other,self)
    def __rdiv__(self,other): return div_proxy(other,self)
    def __rmod__(self,other): return mod(other,self)
    def __rpow__(self,other): return pow(other,self)

    #TRANSPOSE
    T = property(lambda self: transpose(self))

    shape = property(lambda self: shape(self))
    def reshape(self, shape, ndim=None):
        """Return a reshaped view/copy of this variable.

        :param shape: something that can be converted to a symbolic vector of integers

        :param ndim: the length of the shape.  Passing None here means for theano to try and
        guess the length of `shape`.
        """
        return reshape(self, shape, ndim=ndim)

    def dimshuffle(self, *pattern):
        """Reorder the dimensions of this variable, optionally inserting broadcasted dimensions.

        :param pattern: list/tuple of int mixed with 'x' for broadcastable dimensions

        For example, to create a 3D view of a [2D] matrix, call ``dimshuffle([0,'x',1])``.  This
        will create a 3D view such that the middle dimension is an implicit broadcasted
        dimension.  To do the same thing on the transpose of that matrix, call ``dimshuffle([1,
        'x', 0])``.

        This function supports the pattern passed as a tuple, or as a variable-length argument (e.g. ``a.dimshuffle(pattern)`` is equivalent to ``a.dimshuffle(*pattern)`` where ``pattern`` is a list/tuple of ints mixed with 'x' characters).

        For more information, see `DimShuffle`.
        """
        if (len(pattern) == 1) and (isinstance(pattern[0], (list, tuple))):
            pattern = pattern[0]
        op = DimShuffle(list(self.type.broadcastable), pattern)
        return op(self)

    def flatten(self, ndim=1):
        return flatten(self, ndim)

    #SLICING
#     def __getitem__(self, args): return Subtensor.from_idxs(self,
#             args).outputs[0]
#     def __getslice__(self, *args): return Subtensor.from_idxs(self,
#             (slice(*args),)).outputs[0]
    def __getitem__(self, args):
        if not isinstance(args, tuple):
            args = args,
        # Determine if advanced indexing is needed or not
        # The logic is already in Subtensor.convert: if it succeeds,
        # standard indexing is used, else, advanced indexing
        advanced = False
        for arg in args:
            try:
                Subtensor.convert(arg)
            except TypeError:
                advanced = True
                break

        if advanced:
            if config.experimental.advanced_indexing:
                if len(args) == 1:
                    return AdvancedSubtensor1()(self, *args)
                else:
                    return AdvancedSubtensor(args)(self, *args)
            else:
                return AdvancedSubtensor(args)(self, *args)
        else:
            return Subtensor(args)(self, *Subtensor.collapse(args, lambda entry: isinstance(entry, Variable)))

    def __getslice__(self, *args):
        args = slice(*args),
        return self.__getitem__(args)
    
    #COPYING
    def copy(self):
        return tensor_copy(self)

    def __iter__(self): 
        try:
            for i in xrange(get_vector_length(self)):
                yield self[i]
        except:
            # This prevents accidental iteration via builtin.sum(self)
            raise TypeError('TensorType does not support iteration. '
            'Maybe you are using builtin.sum instead of theano.tensor.sum? (Maybe .max?)')
        

    # CONVENIENT ACCESS TO TYPE PROPERTIES
    ndim = property(lambda self: self.type.ndim)
    """The rank of this tensor."""
    broadcastable = property(lambda self: self.type.broadcastable)
    """The broadcastable signature of this tensor.

    See :doc:`broadcasting` for details.
    
    """
    dtype = property(lambda self: self.type.dtype)
    """ The dtype of this tensor.  """

    #extra pseudo-operator symbols
    def __dot__(left, right): return dot(left, right)
    def __rdot__(right, left): return dot(left, right)

    def sum(self, axis=None):
        return elemwise.Sum(axis)(self)

    def norm(self, L, axis=None):
        if L==0:
            raise NotImplementedError()
        if L==float('inf'):
            raise NotImplementedError()
        #optimizations will/should catch cases like L=1, L=2
        return pow(pow(abs_(self), L).sum(axis=axis), 1.0/L)

    def mean(self, axis=None):
        """See `theano.tensor.mean`"""
        return mean(self, axis)

    def var(self, axis=None):
        """See `theano.tensor.var`"""
        return var(self, axis)

    def min(self, axis=None):
        """See `theano.tensor.min`"""
        return min(self, axis)

    def max(self, axis=None):
        """See `theano.tensor.max`"""
        return max(self, axis)

    #TO TRUMP NUMPY OPERATORS
    __array_priority__ = 1000


    def get_constant_value(self):
        return get_constant_value(self)
    
class TensorVariable(Variable, _tensor_py_operators):
    """Subclass to add the tensor operators to the basic `Variable` class."""
TensorType.Variable = TensorVariable

class TensorConstantSignature(tuple):
    """A Signature object for comparing TensorConstant instances

    An instance is a pair: (Type instance, ndarray).
    """
    def __eq__(self, other):
        if type(self) != type(other):
            return False
        try:
            (t0, d0), (t1,d1) = self, other
        except:
            return False
        #N.B. compare shape to ensure no broadcasting in ==
        #N.B. compare elementwise last because it is the most expensive check
        return (t0 == t1) and (d0.shape == d1.shape) \
                and (self.sum == other.sum) and (numpy.all(d0 == d1)) 
    def __hash__(self):
        t, d = self
        return hashtype(self) ^ hash(t) ^ hash(d.shape) ^ hash(self.sum)
    def _get_sum(self):
        try:
            return self._sum
        except:
            self._sum = self[1].sum()
        return self._sum
    sum = property(_get_sum)


class TensorConstant(Constant, _tensor_py_operators):
    """Subclass to add the tensor operators to the basic `Constant` class.
    
    To create a TensorConstant, use the `constant` function in this module.
    """
    def signature(self):
        return TensorConstantSignature((self.type, self.data))
TensorType.Constant = TensorConstant

class TensorValue(Value, _tensor_py_operators):
    """Subclass to add the tensor operators to the basic `Value` class.
    
    To create a TensorValue, use the `value` function in this module.

    :note: Value is deprecated by SharedVariable
    """


Tensor = TensorType

#QUESTION: why are we doing this!?
elemwise.as_tensor_variable = as_tensor_variable
elemwise.TensorType = TensorType
elemwise.TensorVariable = TensorVariable
elemwise.TensorConstant = TensorConstant
elemwise.TensorValue = TensorValue



#########################
# Utilities
#########################

def _elemwise(scalar_op, name, doc_prefix=''):
    straight = elemwise.Elemwise(scalar_op, name = name)
    inplace_scalar_op = scalar_op.__class__(scal.transfer_type(0))
    inplace = elemwise.Elemwise(inplace_scalar_op, {0: 0}, name = name+"_inplace")

    # don't add the inplace versions, they aren't supposed to be part of the user interface
    _constructor_list.append(straight) 
    
    # This is here so that gen_oplist can detect which module declared these variables.

    straight.__module__ = 'tensor'
    inplace.__module__ = 'tensor'

    if doc_prefix:
        straight.__doc__ = doc_prefix + '\n' + straight.__doc__

    return straight, inplace

def _redefine(real_symbol_value, module='tensor'):
    """Replace the value associated with a function symbol.
    
    This is useful to trick epydoc into doing what we want.  It's a hack.
    """
    real_symbol_value.__module__ = 'tensor.basic'
    def decorator(f):
        return real_symbol_value
    return decorator

def _redefine_asRoutine(real_symbol_value):
    real_symbol_value.__epydoc_asRoutine = True
    def decorator(f):
        return real_symbol_value
    return decorator

def _scal_elemwise(symbol):
    """Replace a symbol definition with an elementwise version of the corresponding scalar Op"""
    symbolname = symbol.__name__
    inplace = symbolname.endswith('_inplace')
    if inplace:
      msg = "inplace"
    else:
      msg = "no_inplace"
    n="Elemwise{%s,%s}"%(symbolname,msg)

    if inplace:
        scalar_op = getattr(scal, symbolname[:-len('_inplace')])
        inplace_scalar_op = scalar_op.__class__(scal.transfer_type(0))
        rval = elemwise.Elemwise(inplace_scalar_op, {0: 0}, name=n)
    else:
        scalar_op = getattr(scal, symbolname)
        rval = elemwise.Elemwise(scalar_op, name=n)

    if getattr(symbol, '__doc__', False):
        rval.__doc__ = symbol.__doc__ + '\n' + rval.__doc__

    #for the meaning of this see the ./epydoc script
    # it makes epydoc display rval as if it were a function, not an object
    rval.__epydoc_asRoutine = symbol
    rval.__module__ = 'tensor'

    pprint.assign(rval, printing.FunctionPrinter(symbolname))

    return rval


#########################
# Casting Operations
#########################

class TensorFromScalar(Op):
    def make_node(self, s):
        assert isinstance(s.type, scal.Scalar)
        return Apply(self,
                     [s],
                     [tensor(dtype = s.type.dtype,
                             broadcastable = ())])
    def perform(self, node, (s, ), (out, )):
        out[0] = numpy.asarray(s)
    def grad(self, (s,), (dt,)):
        return [scalar_from_tensor(dt)]
tensor_from_scalar = TensorFromScalar()

class ScalarFromTensor(Op):
    def make_node(self, t):
        assert isinstance(t.type, TensorType)
        assert t.type.broadcastable == ()
        return Apply(self,
                     [t],
                     [scal.Scalar(dtype = t.type.dtype).make_variable()])
    def perform(self, node, (s, ), (out, )):
        out[0] = s.flatten()[0]
    def grad(self, (s,), (dt,)):
        return [tensor_from_scalar(dt)]
    def __str__(self):
        return self.__class__.__name__
scalar_from_tensor = ScalarFromTensor()


#to be removed as we get the epydoc routine-documenting thing going -JB 20080924
def _conversion(real_value, name):
    __oplist_tag(real_value, 'casting')
    real_value.__module__='tensor.basic'
    pprint.assign(real_value, printing.FunctionPrinter(name))
    return real_value


#
#  These _conver_to_<type> functions have leading underscores to indicate that they should not
#  be called directly.  They do not perform sanity checks about what types you are casting to
#  what.  That logic is implemented by the `cast()` function below.
#

_convert_to_int8  = _conversion(elemwise.Elemwise(scal.convert_to_int8), 'int8')
"""Cast to 8-bit integer"""
    
_convert_to_int16 = _conversion(elemwise.Elemwise(scal.convert_to_int16), 'int16')
"""Cast to 16-bit integer"""

_convert_to_int32 = _conversion(elemwise.Elemwise(scal.convert_to_int32), 'int32')
"""Cast to 32-bit integer"""

_convert_to_int64 = _conversion(elemwise.Elemwise(scal.convert_to_int64), 'int64')
"""Cast to 64-bit integer"""

_convert_to_uint8  = _conversion(elemwise.Elemwise(scal.convert_to_uint8), 'uint8')
"""Cast to unsigned 8-bit integer"""
    
_convert_to_uint16 = _conversion(elemwise.Elemwise(scal.convert_to_uint16), 'uint16')
"""Cast to unsigned 16-bit integer"""

_convert_to_uint32 = _conversion(elemwise.Elemwise(scal.convert_to_uint32), 'uint32')
"""Cast to unsigned 32-bit integer"""

_convert_to_uint64 = _conversion(elemwise.Elemwise(scal.convert_to_uint64), 'uint64')
"""Cast to unsigned 64-bit integer"""

_convert_to_float32 = _conversion(elemwise.Elemwise(scal.convert_to_float32), 'float32')
"""Cast to single-precision floating point"""

_convert_to_float64 = _conversion(elemwise.Elemwise(scal.convert_to_float64), 'float64')
"""Cast to double-precision floating point"""

_convert_to_complex64  = _conversion(elemwise.Elemwise(scal.convert_to_complex64), 'complex64')
"""Cast to single-precision complex"""

_convert_to_complex128 = _conversion(elemwise.Elemwise(scal.convert_to_complex128), 'complex128')
"""Cast to double-precision complex"""

_cast_mapping = {
           'int8': _convert_to_int8,
           'int16': _convert_to_int16,
           'int32': _convert_to_int32,
           'int64': _convert_to_int64,
           'uint8': _convert_to_uint8,
           'uint16': _convert_to_uint16,
           'uint32': _convert_to_uint32,
           'uint64': _convert_to_uint64,
           'float32': _convert_to_float32,
           'float64': _convert_to_float64,
           'complex64': _convert_to_complex64,
           'complex128': _convert_to_complex128}
@constructor
def cast(x, dtype):
    """Symbolically cast `x` to a Tensor of type `dtype`.""" 
    if dtype=='floatX': dtype = config.floatX
    
    _x = as_tensor_variable(x)
    if _x.type.dtype == dtype:
        return _x
    if _x.type.dtype.startswith('complex') and not dtype.startswith('complex'):
        raise TypeError('Casting from complex to real is ambiguous: consider real(), imag(), angle() or abs()')
    return _cast_mapping[dtype](x)



##########################
# Unary Operations
##########################

class Shape(Op):
    """
    L{Op} to return the shape of a matrix.

    @note: Non-differentiable.
    """
    def __hash__(self):
        return hash(type(self))
    def __eq__(self, other):
        return type(self) == type(other)
    def __str__(self):
        return self.__class__.__name__
    def make_node(self, x):
        x = as_tensor_variable(x)
        return Apply(self, [x], [lvector()])
    def perform(self, node, (x, ), (out, )):
        out[0] = theano._asarray(x.shape, dtype = 'int64')
    def grad(self, (x,), (gz,)):
        return [None]
@constructor
def old_shape(a):
    """Return the shape tuple of a TensorType Variable, it may be either symbolic or nonsymbolic.

    If the shape of the expression is not known at graph-construction time, then a symbolic
    lvector will be returned, corresponding to the actual shape at graph-execution time.
    """
    va = as_tensor_variable(a)
    #print 'HERE', va, va.type
    if None in va.type.shape:
        # Some shape components are unknown at this time
        return _shape(va)
    else:
        # all shape components are known at compile time, so we return
        # a tuple directly.  This tuple is like the numpy.ndarray.shape tuple.
        return va.type.shape

shape = Shape()
_shape = shape #was used in the past, now use shape directly.
pprint.assign(_shape, printing.MemberPrinter('shape'))

class MaxAndArgmax(Op):
    """Calculate the max and argmax over a given axis.
    
    .. note::

        If axis is None it means to calculate the max over the last dimension which is
        DIFFERENT FROM NUMPY!!
        To have the behavior of numpy do a flatten of the input before passing the data to this op.
        If the input to flatten is not ccontiguous, this will make a copy to a contiguous version.
    """
    nin=2 # tensor, axis
    nout=2 # max val, max idx
    E_axis = 'invalid axis'
    
    def make_node(self, x, axis=None):
        x = _as_tensor_variable(x)
        if axis is None:
            axis = x.type.ndim - 1
        if isinstance(axis,int):
            axis = [axis]
        #we make the axis all positive to make the infer_shape work with negative axis
        if x.type.ndim>0:
            for id,a in enumerate(axis):
                if not isinstance(a, TensorVariable) and a<0:
                    if -a>x.type.ndim:
                      raise ValueError('axis out of range')
                    axis[id]=x.type.ndim+a
        axis = _as_tensor_variable(axis)
        inputs = [x, axis]
        #TODO: figure things out if axis is a constant
        broadcastable = [False] * (x.type.ndim - 1)
        outputs = [tensor(x.type.dtype, broadcastable,name='max'), 
                   tensor('int32', broadcastable,name='argmax')]
        return Apply(self, inputs, outputs)
    def perform(self, node, (x, axis), (max, max_idx)):
        max[0] = numpy.asarray(numpy.max(x, axis))
        max_idx[0] = theano._asarray(numpy.argmax(x, axis), dtype='int32')

    def infer_shape(self, node, (ishape,axis_shape)):
        axis=node.inputs[1]
        if axis is None:
            return [(),()]
        rval = tuple([ishape[i] for (i,b) in enumerate(node.inputs[0].type.broadcastable) if i !=axis.data])
        return [rval,rval]

    def grad(self, (x, axis), (g_max, g_max_idx)):
        # @warning: This only works if axis is 0, else the max is
        # broadcasted wrong in the call to eq.
        # @note: This function should work correctly for L{vector}s.
#        (x, y), (gz, gw)
#        gz*dz/dx + gw*dw/dx, gz*dz/dy + gw*dw/dy
#        gMax * dMax/dx + gArgMax * dArgMax/dx, gMax * dMax/daxis + gArgMax * dArgMax/daxis
#       g_max has one less dimension than x, so you need to complete g_max to x's shape
#        when axis=0 the broadcasting mechanism does it automatically
        
        if not ( axis.data == 0 or axis.data == x.ndim-1):
            raise NotImplementedError('MaxAndArgmax gradient with axis corresponding to internal dimension')
        if axis.data==0:
          g_max_pad = shape_padleft(g_max)
        else:
           g_max_pad = shape_padright(g_max)
        xmax = max(x, axis)
        if axis.data==0:
          xmax_pad = shape_padleft(xmax)
        else:
          xmax_pad = shape_padright(xmax)
        g_x = eq(xmax_pad, x) * g_max_pad
        return g_x, None
    def __str__(self):
      return self.__class__.__name__

_max_and_argmax = MaxAndArgmax()
@_redefine_asRoutine(_max_and_argmax)
def max_and_argmax(a):
    pass


@constructor
def max(x, axis=None):
    """
    Return maximum elements obtained by iterating over given axis

    Default axis is the last one.
    """
    # In python (using MaxAndArgmax.perform()) this leads to an wasteful
    # implementation that goes through the data twice instead of once
    # but when Argmax.c_impl() is in place, it should be fine.
    return max_and_argmax(x,axis)[0]

@constructor
def argmax(x, axis=None):
    """
    Return indexes of maximum elements obtained by iterating over given axis

    Default axis is the last one.
    """
    # In python (using MaxAndArgmax.perform()) this leads to an wasteful
    # implementation that goes through the data twice instead of once
    # but when Argmax.c_impl() is in place, it should be fine.
    return max_and_argmax(x,axis)[1]

@constructor
def min(x, axis=None):
    str_x_type = str(x.dtype)
    if str_x_type.startswith('float') or str_x_type.startswith('int'):
        return -max(-x, axis=axis)
    else:
        #Be careful about unsigned integers, complex
        raise NotImplementedError()

@constructor
def argmin(x, axis=None):
    str_x_type = str(x.dtype)
    if str_x_type.startswith('float') or str_x_type.startswith('int'):
        return argmax(-x, axis=axis)
    else:
        #Be careful about unsigned integers, complex
        raise NotImplementedError()

@constructor
def smallest(*args):
    """Return the [elementwise] smallest of a variable number of arguments (like python's min)."""
    if len(args) == 2:
        a, b = args
        return switch(a < b, a, b)
    else:
        return min(stack(*args), axis=0)

@constructor
def largest(*args):
    """Return the [elementwise] largest of a variable number of arguments (like python's max)."""
    if len(args) == 2:
        a, b = args
        return switch(a > b, a, b)
    else:
        return max(stack(*args), axis=0)


##########################
# Comparison
##########################

@_scal_elemwise
def lt(a, b):
    """a < b"""

@_scal_elemwise
def gt(a, b):
    """a > b"""

@_scal_elemwise
def le(a, b):
    """a <= b"""

@_scal_elemwise
def ge(a, b):
    """a >= b"""

@_scal_elemwise
def eq(a, b):
    """a == b"""

@_scal_elemwise
def neq(a, b):
    """a != b"""


##########################
# Condition
##########################

@_scal_elemwise
def switch(cond, ift, iff):
    """if cond then ift else iff"""


##########################
# Bit-wise
##########################

@_scal_elemwise
def and_(a,b):
    """bitwise a & b"""

@_scal_elemwise
def or_(a,b):
    """bitwise a | b"""

@_scal_elemwise
def xor(a,b):
    """bitwise a ^ b"""

@_scal_elemwise
def invert(a):
    """bitwise ~a"""

##########################
# Math
##########################

@_scal_elemwise
def abs_(a):
    """|`a`|

    TensorVariable overloads the `TensorVariable.__abs__` operator so that
    this function is called when you type abs(a).

    """

pprint.assign(abs_, printing.PatternPrinter(('|%(0)s|', -1000)))

@_scal_elemwise
def exp(a):
    """e^`a`"""

@_scal_elemwise
def neg(a):
    """-a"""

@_scal_elemwise
def inv(a):
    """1.0/a"""

@_scal_elemwise
def log(a):
    """base e logarithm of a"""

@_scal_elemwise
def log2(a):
    """base 2 logarithm of a"""

@_scal_elemwise
def log10(a):
    """base 10 logarithm of a"""

@_scal_elemwise
def log1p(a):
    """log(1+a)"""

@_scal_elemwise
def sgn(a):
    """sign of a"""

@_scal_elemwise
def ceil(a):
    """ceiling of a"""

@_scal_elemwise
def floor(a):
    """floor of a"""

@_scal_elemwise
def iround(a):
    """int(round(a))"""

@_scal_elemwise
def sqr(a):
    """square of a"""

@_scal_elemwise
def sqrt(a):
    """square root of a"""

@_scal_elemwise
def cos(a):
    """cosine of a"""

@_scal_elemwise
def sin(a):
    """sine of a"""

@_scal_elemwise
def tan(a):
    """tangent of a"""

@_scal_elemwise
def cosh(a):
    """hyperbolic cosine of a"""

@_scal_elemwise
def sinh(a):
    """hyperbolic sine of a"""

@_scal_elemwise
def tanh(a):
    """hyperbolic tangent of a"""

class Real(Op):
    """Extract the real elements of a complex ndarray"""
    view_map = {0:[0]}
    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return hash(type(self))
    def make_node(self, x):
        _x = as_tensor(x)
        y_dtype = _x.type.dtype
        if y_dtype == 'complex64':
            y_dtype = 'float32'
        if y_dtype == 'complex128':
            y_dtype = 'float64'
        _y = Tensor(y_dtype, _x.type.broadcastable)()
        return Apply(self, [_x], [_y])
    def perform(self, node, (x,), (y,)):
        if str(x.dtype).startswith('complex'):
            y[0] = x.real
        else:
            y[0] = x
    def grad(self, inputs, (g_y,)):
        #TODO: waiting on a Complex(real=, imag=) op that can merge
        #things back into a complex tensor
        raise NotImplementedError()
_real = Real()
@constructor
def real(x):
    """Return the real part of real or complex-valued `x`

    For real-valued `x`, `x` itself is returned.
    """
    _x = as_tensor_variable(x)
    if _x.type.dtype.startswith('complex'):
        return _real(x)
    else:
        return _x

class Imag(Op):
    """Extract the imaginary elements of a complex ndarray"""
    view_map = {0:[0]}
    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return hash(type(self))
    def make_node(self, x):
        _x = as_tensor_variable(x)
        if not _x.type.dtype.startswith('complex'):
            raise TypeError('Imag(x) requires complex x', x)
        if _x.type.dtype == 'complex64': y_dtype = 'float32'
        elif _x.type.dtype == 'complex128': y_dtype = 'float64'
        else:
            raise NotImplementedError('what is this?', y_dtype)
        _y = Tensor(y_dtype, _x.type.broadcastable)()
        return Apply(self, [_x], [_y])
    def perform(self, node, (x,), (y,)):
        if str(x.dtype).startswith('complex'):
            y[0] = x.imag
        else:
            y[0] = x * 0
    def grad(self, inputs, (g_y,)):
        # TODO: waiting on a complex(real=, imag=) op that can merge
        # things back into a complex tensor
        raise NotImplementedError()
_imag = Imag()
@constructor
def imag(x):
    """Return the imaginary part of real or complex-valued `x`

    For real-valued 'x' this returns `zeros_like(x)`.
    """
    _x = as_tensor_variable(x)
    if _x.type.dtype.startswith('complex'):
        return _imag(x)
    else:
        return zeros_like(x)

@constructor
def angle(x):
    """Return the angular component of complex-valued `x`"""
    raise NotImplementedError()


##########################
# Misc
##########################

#fill, _fill_inplace = _elemwise(scal.second, 'fill',
    #"""fill WRITEME (elemwise)""")
@_scal_elemwise
def second(a, b):
    """Create a matrix by filling the shape of a with b"""

fill = second
pprint.assign(fill, printing.FunctionPrinter('fill'))


@constructor
def ones_like(model):
    """WRITEME"""
    #return Ones(model.type.ndim)(shape(model))
    ret= fill(model, constant(1.0, dtype=model.type.dtype))
    return ret

@constructor
def zeros_like(model):
    """WRITEME"""
    #return Zeros(model.type.ndim)(shape(model))
    return fill(model, constant(0.0, dtype=model.type.dtype))

class Eye(gof.Op):
    def __init__(self, dtype='float64'):
        self.dtype = dtype
    def make_node(self,n,m,k):
        n = as_tensor_variable(n)
        m = as_tensor_variable(m)
        k = as_tensor_variable(k)
        return gof.Apply(self, [n,m,k], [TensorType(dtype = self.dtype, broadcastable = (False,False))()])

    def perform(self, node, (n,m,k), (out,)):
        out[0] = numpy.eye(n,m,k)

    def grad(self, (n,m,k),(gout,)):
        return [None, None, None]

    def __eq__(self,other):
        return type(self) == type(other) and self.dtype == other.dtype

    def __hash__(self):
        return hash(self.dtype) ^ hash(type(self))


def eye(n, m=None, k = 0, dtype = 'float64'):
    if m == None:
        m = n
    localop = Eye(dtype)
    return localop(n,m,k)

def identity_like(x):
    return eye(x.shape[0], x.shape[1], k=0, dtype = x.dtype)

if 0:
    ## COMMENTED OUT FEB 17 2010
    ## TODO (DOCUMENT AND WRITE TESTS) OR DELETE
    class Filler(gof.Op):
        """WRITEME"""
        def __init__(self, value, ndim, dtype = 'float64'):
            self.value = value
            self.ndim = ndim
            self.dtype = dtype
            self.type = TensorType(dtype = dtype,
                               broadcastable = (False,)*ndim)

        def make_node(self, dims):
            dims = as_tensor_variable(dims)
            return gof.Apply(self, [dims], [self.type()])

        def perform(self, node, (dims,), (out,)):
            if out[0] is not None:
                out[0].resize(dims, refcheck = 0)
                out[0].fill(self.value)
            else:
                if self.value == 0:
                    out[0] = numpy.zeros(dims, dtype = self.dtype)
                elif self.value == 1:
                    out[0] = numpy.ones(dims, dtype = self.dtype)
                else:
                    out[0] = numpy.ones(dims, dtype = self.dtype) * self.value

        def grad(self, (dims,), (gout,)):
            return None,

        def __eq__(self, other):
            return type(self) == type(other) and self.ndim == other.ndim and self.dtype == other.dtype

        def __hash__(self):
            return hash(self.ndim) ^ hash(self.dtype)

    Zeros = partial(Filler, 0)
    """WRITEME"""

    Ones = partial(Filler, 1)
    """WRITEME"""

    @constructor
    def zero():
        """
        Return a scalar zero, e.g. for initializing sums.
        """
        return Zeros(0)([])

    @constructor
    def one():
        """WRITEME"""
        return Ones(0)([])

    pprint.assign(lambda pstate, r: r.owner and isinstance(r.owner.op, Filler) and r.owner.op.value == 0, printing.FunctionPrinter('zeros'))
    pprint.assign(lambda pstate, r: r.owner and isinstance(r.owner.op, Filler) and r.owner.op.value == 1, printing.FunctionPrinter('ones'))

class Alloc(gof.Op):
    """Create a Tensor from an initial value and a desired shape

    alloc(value, shape0, shape1, ..., shapeN) 

    Returns an N-dimensional tensor initialized by `value` using something equivalent to
    >>> z = numpy.zeros(shape, value.dtype)
    >>> z += value

    The result has N dimensions, has the dtype of `value` and is obtained by broadcasting value
    over the output ndarray.

    This Op is used to replace fill() during optimizations because after shapes are lifted, 
    the first argument to fill can often be pruned from the graph.
    """
    def __init__(self):
        pass

    def __eq__(self, other):
        return type(self) == type(other)

    def __hash__(self):
        return hash(type(self))

    def __str__(self):
        return self.__class__.__name__

    def make_node(self, value, *shape):
        v = as_tensor_variable(value)
        sh = [as_tensor_variable(s) for s in shape]
        bcast = []
        for s in sh:
            if s.type.dtype[:3] not in ('int', 'uin'):
                raise TypeError('Shape arguments must be integers', s)
            # if s is constant 1, then we're broadcastable in that dim
            try:
                const_shp = get_constant_value(s)
            except TypeError:
                const_shp = None
            bcast.append(numpy.all(1 == const_shp))
        otype = TensorType(dtype=v.dtype, broadcastable=bcast)
        return gof.Apply(self, [v]+sh, [otype()])

    def perform(self, node, inputs, (out,)):
        v = inputs[0]
        sh = tuple([int(i) for i in inputs[1:]])
        if out[0] is None or out[0].shape != sh:
#            out[0] = numpy.empty(sh, dtype=v.dtype)
            out[0] = numpy.zeros(sh, dtype=v.dtype)
        out[0][...] = v # broadcast v to fill us up

    def infer_shape(self, node, input_shapes):
        return [node.inputs[1:]]

    def grad(self, inputs, (gout,)):
        return [None for i in inputs]

alloc = Alloc()
pprint.assign(alloc, printing.FunctionPrinter('alloc'))

@_redefine(elemwise.Elemwise(scal.identity))
def tensor_copy(a):
    """Create a duplicate of `a` (with duplicated storage)"""
pprint.assign(tensor_copy, printing.IgnorePrinter())


@_redefine(elemwise.Elemwise(scal.identity, inplace_pattern = {0: [0]}))
def view(a):
    """Create a duplicate of `a` (with shared storage)"""

@constructor
def sum(input, axis = None):
    """WRITEME"""
    return elemwise.Sum(axis)(input)

pprint.assign(Sum(), printing.FunctionPrinter('sum'))

@constructor
def prod(input, axis = None):
    """WRITEME"""
    return elemwise.Prod(axis)(input)

class Mean(elemwise.CAReduce):
    def __init__(self, axis = None):
        elemwise.CAReduce.__init__(self, scal.add, axis)
    def __str__(self):
        if self.axis is not None:
            return "Mean{%s}" % (", ".join(str(x) for x in self.axis))
        else:
            return "Mean"

    def _output_dtype(self, idtype):
        # we want to protect against overflow
        return 'float64'

    def perform(self, node, (input, ), (output, )):
      output[0]=numpy.mean(input,axis=self.axis)

    def c_code(self, node, name, inames, onames, sub):
      if self.axis!=None:
        return super(Op, self).c_code(node, name, inames, onames, sub)
      ret = elemwise.CAReduce.c_code(self, node, name, inames, onames, sub)
      #TODO: c_code perform support only axis==None
      return ret + """
*((double *)PyArray_DATA(%s)) /= PyArray_SIZE(%s);
"""%(onames[0],inames[0])

#TODO: implement the grad. When done and tested, you can make this the default version.
#    def grad(self, (x,), (gout,)):
#      import pdb;pdb.set_trace()
#      return grad(mean(x, self.axis, op=False),[x])

@constructor
def mean(input, axis = None, op = False):
    """Compute the mean value along the given axis of a tensor `input`

    :param axis: compute the mean along this axis of the tensor.  None means all axes (like
    numpy).
    :type axis: None or int or (list of int) (see `Sum`)
    
    """
    if op:
      return Mean(axis)(input)

    if str(input.dtype).startswith('int'):
        # we need to cast eventually anyway, and this helps
        # to prevents overflow
        input = cast(input, 'float64')
    s = sum(input, axis)
    shp = shape(input)
    if input.dtype == 'float32':
        shp = cast(shp, 'float32')
    if axis is None:
        axis = range(input.type.ndim)
    elif isinstance(axis, int):
        axis = [axis]
    for i in axis:
        s = s / shp[i]
    if input.dtype.startswith('float'):
        assert input.dtype == s.dtype
    return s

@constructor
def var(input, axis = None):
    """Compute the variance along the given axis of a tensor `input`

    :param axis: compute the variance along this axis of the tensor.  None means trailing axis.
    :type axis: None or int or (list of int) (see `Sum`)

    """
    input_ndim = input.type.ndim
    if axis == None:
        axis = range(input_ndim)
    if isinstance(axis, int):
        axis = [axis]

    #make a pattern that will undo the reduction of dimensions caused by mean
    pattern = []
    next_dim = 0
    for i in range(input_ndim):
        if i in axis:
            pattern.append('x')
        else:
            pattern.append(next_dim)
            next_dim += 1

    #compute the axis-wise mean
    mean_input_reduced = mean(input, axis)

    #broadcast that back out to match input
    mean_input = DimShuffle(
            list(mean_input_reduced.type.broadcastable),
            pattern)(mean_input_reduced)

    #center the input
    centered_input = input - mean_input

    #return the mean sqr
    return mean(centered_input**2, axis)

if 0:
    ## COMMENTED OUT FEB 17 2010
    ## TODO (DOCUMENT AND WRITE TESTS) OR DELETE
    class Repeat(gof.Op):

        def make_node(self, input, repeats, axis):
            assert isinstance(input.type, TensorType)
            assert repeats.type == iscalar
            assert axis.type == iscalar
            broadcastable = []
            for i,x in enumerate(input.broadcastable):
              if i==axis:
                broadcastable += [False]
              else:
                broadcastable += [x]

            type = TensorType(dtype = input.type.dtype, broadcastable = \
                              broadcastable)
            #backport
            #type = TensorType(dtype = input.type.dtype,
            #              broadcastable = [False if i==axis else x for i, x in enumerate(input.broadcastable)])
            return gof.Apply(self, [inputs, repeats, axis], [type()])

        def perform(self, node, (input, repeats, axis), (out, )):
            out[0] = numpy.repeat(input, repeats, axis)

        def grad(self, (input, repeats, axis), (gout, )):
            return add.grad((input, gout), (gout,))[:1]

    repeat = Repeat()

class Default(gof.Op):
    """
    Takes an input x and a default value. If the input is not None, a
    reference to it is returned. If the input is None, a copy of the
    default value is returned instead. The input and the default must
    have exactly the same type.
    """
    view_map = {0: [0]}
    def make_node(self, x, default):
        x, default = as_tensor_variable(x), as_tensor_variable(default)
        if  x.type != default.type:
            raise TypeError('Both default() arguments must have same type', x, default)
        return gof.Apply(self, [x, default], [default.type()])
    def perform(self, node, (x, default), (out, )):
        if x is None:
            # why copy?  Theano can't yet understand out[0] being a view of either x or y,
            # so we can be a view of x, but only a copy of y.
            out[0] = default.copy() 
        else:
            out[0] = x
default = Default()
setdefault = default # legacy


##########################
# Arithmetics
##########################

def div_proxy(x, y):
    """Proxy for either true_div or int_div, depending on types of x, y.
    """
    if as_tensor_variable(x).type.dtype.startswith('int') and as_tensor_variable(y).type.dtype.startswith('int'):
        return int_div(x, y)
    else:
        return true_div(x, y)

@_scal_elemwise
def add(a, *other_terms):
    """elementwise addition"""
    # see decorator for function body

@_scal_elemwise
def sub(a, b):
    """elementwise subtraction"""
    # see decorator for function body

@_scal_elemwise
def mul(a, *other_terms):
    """elementwise multiplication"""
    # see decorator for function body

@_scal_elemwise
def true_div(a, b):
    """elementwise [true] division (inverse of multiplication)"""
    # see decorator for function body

@_scal_elemwise
def int_div(a, b):
    """elementwise integer-division"""
    # see decorator for function body

@_scal_elemwise
def mod(a, b):
    """elementwise modulo"""
    # see decorator for function body

@_scal_elemwise
def pow(a, b):
    """elementwise power"""
    # see decorator for function body

@_scal_elemwise
def clip(x, min, max):
    """clip x to be between min and max"""
    # see decorator for function body

pprint.assign(add, printing.OperatorPrinter('+', -2, 'either'))
pprint.assign(mul, printing.OperatorPrinter('*', -1, 'either'))
pprint.assign(sub, printing.OperatorPrinter('-', -2, 'left'))
pprint.assign(neg, printing.OperatorPrinter('-',  0, 'either'))
pprint.assign(true_div, printing.OperatorPrinter('/', -1, 'left'))
pprint.assign(int_div, printing.OperatorPrinter('//', -1, 'left'))
pprint.assign(pow, printing.OperatorPrinter('**', 1, 'right'))



##########################
# View Operations
##########################

def transpose(x, **kwargs):
    dims = range(x.ndim-1, -1, -1)
    return DimShuffle(x.broadcastable, dims, inplace=True)(tensor_copy(x))


class Subtensor(Op):
    """Return a subtensor view

    This class uses a relatively complex internal representation of the inputs
    to remember how the input tensor x should be sliced.  The instance variable
    idxlist is a list whose elements are either integers, or slices.  The
    integers are indexes into the inputs array, and the start/stop/step members
    of each slice are also integer indexes into the inputs array (or None).  The
    inputs array is the tensor x, followed by scalar integer variables.
    
    @todo: add support for advanced tensor indexing (in Subtensor_dx too).

    The idx_list is a tuple similar in structure to the sort of key you might expect in numpy's
    basic indexing mode.  It has one element for each explicitly named dimension.  In numpy, the elements
    can be either  integers or slices containing integers and None.  In Subtensor, each element
    can additionally be a Scalar instance, and slice components can also be Scalar instances
    too.
    """
    e_invalid = 'The index list is longer than the number of dimensions of the tensor.'
    e_subslice = 'nested slicing is not supported'
    e_indextype = "Invalid index type or slice for Subtensor"
    debug = 0

    view_map = {0: [0]}

    @staticmethod
    def collapse(idxs, cond):
        ret = []
        def helper(entry):
            if cond(entry):
                ret.append(entry)
            elif isinstance(entry, slice):
                helper(entry.start)
                helper(entry.stop)
                helper(entry.step)
        for idx in idxs:
            helper(idx)
        return ret

    @staticmethod
    def convert(entry, slice_ok=True):
      scal_types = [scal.int64, scal.int32, scal.int16, scal.int8]
      tensor_types = [bscalar, iscalar, lscalar]
      if isinstance(entry, gof.Variable) and entry.type in scal_types:
        return entry.type
      elif isinstance(entry, gof.Type) and entry in scal_types:
        return entry
      if isinstance(entry, gof.Variable) and entry.type in tensor_types and numpy.all(entry.type.broadcastable):
        return scal.Scalar(entry.type.dtype)
      elif isinstance(entry, gof.Type) and entry in tensor_types and numpy.all(entry.broadcastable):
        return scal.Scalar(entry.dtype)
      elif slice_ok and isinstance(entry, slice):
        a = entry.start
        b = entry.stop
        c = entry.step

        if a is not None:
          slice_a = Subtensor.convert(a, False)
        else:
          slice_a = None

        if b is not None:
           slice_b = Subtensor.convert(b, False)
        else:
           slice_b = None

        if c is not None:
           slice_c = Subtensor.convert(c, False)
        else:
           slice_c = None

        return slice(slice_a,slice_b,slice_c)
          #backport
          #return slice(Subtensor.convert(a, False) if a is not None else None,
            #             Subtensor.convert(b, False) if b is not None else None,
            #             Subtensor.convert(c, False) if c is not None else None)

      elif isinstance(entry, int):
        return entry
      else:
        raise TypeError(Subtensor.e_indextype, entry)

    def __init__(self, idx_list):
        self.idx_list = map(self.convert, idx_list)
        self.perform_cache_cdata = None

    @staticmethod
    def my_as_scalar(a):
        # Since scal.as_scalar does not know about tensor types (it would
        # create a circular import) , this method converts either a
        # TensorVariable or a ScalarVariable to a scalar.
        if isinstance(a, gof.Variable) and isinstance(a.type, TensorType):
            return scalar_from_tensor(a)
        else:
            return scal.as_scalar(a)


    def make_node(self, x, *inputs):
        x = as_tensor_variable(x)
        inputs = tuple(self.my_as_scalar(a) for a in inputs)
        
        idx_list = list(self.idx_list)
        if len(idx_list) > x.type.ndim:
            raise ValueError(Subtensor.e_invalid,
                             (len(idx_list), x.type.ndim))

        #infer the broadcasting pattern
        padded = idx_list + [slice(0,sys.maxint,1)] * (x.type.ndim - len(idx_list))
        broadcastable = [bc for p, bc in zip(padded, x.type.broadcastable) if isinstance(p, slice)]

        input_types = Subtensor.collapse(idx_list, lambda entry: isinstance(entry, gof.Type))
        if len(inputs) != len(input_types):
            raise IndexError("Not enough inputs to fill in the Subtensor template.", inputs, idx_list)
        for input, expected_type in zip(inputs, input_types):
            if input.type != expected_type:
                raise TypeError("Wrong type for the Subtensor template. Expected %s, got %s." % (input.type, expected_type))

        return gof.Apply(self,
                         (x, ) + inputs,
                         [tensor(dtype = x.type.dtype,
                                 broadcastable = broadcastable)])

    def perform(self, node, inputs, (out, )):
        x = inputs[0]

        # The subtensor (or idx_list) does not depend on the inputs.
        # (and cdata was cached on initial call)
        if self.perform_cache_cdata is not None:
            out[0] = numpy.asarray(x.__getitem__(self.perform_cache_cdata))
            return

        indices = list(reversed(inputs[1:]))

        # The subtensor (or idx_list) does not depend on the inputs.
        # (first call caches cdata here)
        if len(indices) == 0:
            cdata = tuple(self.idx_list)
            if len(cdata) == 1:
                cdata = cdata[0]
            self.perform_cache_cdata = cdata
        # General case
        else:
            def convert(entry):
                if isinstance(entry, gof.Type):
                    return indices.pop()
                elif isinstance(entry, slice):
                    return slice(convert(entry.start),
                             convert(entry.stop),
                             convert(entry.step))
                else:
                    return entry
            cdata = tuple(map(convert, self.idx_list))

            if len(cdata) == 1:
                cdata = cdata[0]

        out[0] = numpy.asarray(x.__getitem__(cdata))

    def infer_shape(self, node, shapes):
        xshp = shapes[0]
        assert len(xshp) == node.inputs[0].ndim
        outshp = []
        padded = self.idx_list + [slice(None, None, None)] * (len(xshp) - len(self.idx_list))
        i = 0
        shape_i = node.env.shape_feature.shape_i
        for idx, xl in zip(padded, xshp):
            if isinstance(idx, slice):
                # If it is the default (None, None, None) slice, or a variant,
                # the shape will be xl
                if (idx.start is None or idx.start == 0)\
                    and (idx.stop is None or idx.stop == sys.maxint)\
                    and (idx.step is None or idx.step == 1):
                        outshp.append(xl)
                else:
                    #No easy way to compute the shape
                    outshp.append(shape_i(i)(node.outputs[0]))
                i += 1
            else:
                # That dimension is dropped
                pass
        assert i == node.outputs[0].ndim
        assert len(outshp) == node.outputs[0].ndim
        return [outshp]

    def grad(self, inputs, (gz,)):
        x = inputs[0]
        rest = inputs[1:]
        return [IncSubtensor(self.idx_list)(zeros_like(x), gz, *rest)] + [None] * len(rest)

    def __eq__(self, other):
        return type(self) == type(other) and self.idx_list == other.idx_list

    def __hash__(self):
        #TODO: optimize by cache this hash value
        msg = []
        for entry in self.idx_list:
          if isinstance(entry, slice):
            msg += [(entry.start, entry.stop, entry.step)]
          else:
            msg += [entry]
        
        idx_list = tuple(msg)
        #backport
        #idx_list = tuple((entry.start, entry.stop, entry.step)
        #                 if isinstance(entry, slice)
        #                 else entry
        #                 for entry in self.idx_list)
        return hash(idx_list)

    @staticmethod
    def str_from_slice(entry):
        msg = []
        for x in [entry.start, entry.stop, entry.step]:
            if x is None:
                msg.append("")
            else:
                msg.append(str(x))
        return ":".join(msg)
    def __str__(self):
        indices = []
        for entry in self.idx_list:
            if isinstance(entry, slice):
                indices.append(self.str_from_slice(entry))
            else:
                indices.append(str(entry))
        return "%s{%s}" % (self.__class__.__name__, ", ".join(indices))



class SubtensorPrinter:

    def process(self, r, pstate):
        if r.owner is None:
            raise TypeError("Can only print Subtensor.")
        elif isinstance(r.owner.op, Subtensor):
            idxs = r.owner.op.idx_list
            inputs = list(r.owner.inputs)
            input = inputs.pop()
            sidxs = []
            inbrack_pstate = pstate.clone(precedence = -1000)
            for entry in idxs:
                if isinstance(entry, int):
                    sidxs.append(str(entry))
                elif isinstance(entry, scal.Scalar):
                    sidxs.append(inbrack_pstate.pprinter.process(inputs.pop()))
                elif isinstance(entry, slice):
                    if entry.start is None or entry.start==0:
                      msg1 = ""
                    else:
                      msg1 =  entry.start

                    if entry.stop is None or entry.stop == sys.maxint:
                      msg2 = ""
                    else:
                      msg2 =  entry.stop

                    if entry.step is None:
                      msg3 = ""
                    else:
                      msg3 =  ":%s" % entry.step
                    
                    sidxs.append("%s:%s%s"  % (msg1, msg2, msg3))
                    #backport
                    #sidxs.append("%s:%s%s" % ("" if entry.start is None or entry.start == 0 else entry.start,
                    #                          "" if entry.stop is None or entry.stop == sys.maxint else entry.stop,
                    #                          "" if entry.step is None else ":%s" % entry.step))
            return "%s[%s]" % (pstate.pprinter.process(input, pstate.clone(precedence = 1000)), ", ".join(sidxs))
        else:
            raise TypeError("Can only print Subtensor.")

pprint.assign(lambda pstate, r: r.owner and isinstance(r.owner.op, Subtensor), SubtensorPrinter())

def setsubtensor(x, y, idx_list, inplace=False):
    """
    setsubtensor is meant to replicate the following numpy behaviour: x[i,j,k] = y

    :param x: symbolic variable for the lvalue of = operation
    :param y: symbolic variable for the rvalue of = operation
    :param idx_list: tuple of length x.dim, containing indices with which to index x.
    :param inplace: boolean to declare whether the operation is in place or not (False unless
     called from within an optimization)

    :Details: idx_list can be a tuple containing a mixture of numeric constants, symbolic
     scalar values and standard numpy slice objects. i.e: 
     idx_list=(1,2,3), idx_list=(1,b,3) where b is an iscalar variable, 
     idx_list=(slice(start,stop,step),b,3) equivalent to x[start:stop:step, b, 3]
    """
    the_op = IncSubtensor(idx_list, inplace, True)
    return the_op(x, y, *Subtensor.collapse(idx_list, lambda entry: isinstance(entry, Variable)))

def incsubtensor(x, y, idx_list, inplace=False):
    """
    incsubtensor is meant to replicate the following numpy behaviour: x[i,j,k] += y

    :see: theano.tensor.basic.setsubtensor
   """
    the_op = IncSubtensor(idx_list, inplace, False)
    return the_op(x, y, *Subtensor.collapse(idx_list, lambda entry: isinstance(entry, Variable)))

class IncSubtensor(Op):
    """Increment a subtensor.

    This is like numpy's 

        z[i,j,k] += <something> 
    
    It is used internally to implement the gradient on SubTensor.

    :param set_instead_of_inc: if True set the subtensor to the value instead
    of incrementing it by that value.
    """

    def __init__(self, idx_list, inplace=False, set_instead_of_inc=False):
        self.idx_list = map(Subtensor.convert, idx_list)
        self.inplace = inplace
        if inplace:
            self.destroy_map = {0: [0]}
        self.set_instead_of_inc = set_instead_of_inc

    def __eq__(self, other):
        return type(self) == type(other) \
                and self.idx_list == other.idx_list \
                and self.inplace == other.inplace \
                and self.set_instead_of_inc == other.set_instead_of_inc

    def __hash__(self):
        msg = []
        for entry in self.idx_list:
          if isinstance(entry, slice):
            msg += [(entry.start, entry.stop, entry.step)]
          else:
            msg += [entry]

        idx_list = tuple(msg)
        #backport
        #idx_list = tuple((entry.start, entry.stop, entry.step)
        #                 if isinstance(entry, slice)
        #                 else entry
        #                 for entry in self.idx_list)
        return hashtype(self) ^ hash(idx_list) ^ hash(self.inplace) \
                        ^ hash(self.set_instead_of_inc)

    def __str__(self):
        indices = []
        for entry in self.idx_list:
            if isinstance(entry, slice):
                indices.append(Subtensor.str_from_slice(entry))
            else:
                indices.append(str(entry))
        if self.inplace:
            msg = 'Inplace'
        else:
            msg = ''
        if not self.set_instead_of_inc:
            msg += 'Inc'
        else:
            msg += 'Set'
        return  "%s%s{%s}" % (msg,
                self.__class__.__name__, ", ".join(indices))

    def make_node(self, x, y, *inputs):
        x, y = map(as_tensor_variable, [x, y])
        inputs = tuple(map(Subtensor.my_as_scalar, inputs))
        
        idx_list = list(self.idx_list)
        if len(idx_list) > x.type.ndim:
            raise ValueError(Subtensor.e_invalid,
                             (len(idx_list), x.type.ndim))

        #infer the broadcasting pattern
        padded = idx_list + [slice(0,sys.maxint,1)] * (x.type.ndim - len(idx_list))
        broadcastable = [bc for p, bc in zip(padded, x.type.broadcastable) if isinstance(p, slice)]

        #if y.type.broadcastable != tuple(broadcastable):
        #    raise TypeError("Invalid broadcastable pattern for y in IncSubtensor.make_node")

        input_types = Subtensor.collapse(idx_list, lambda entry: isinstance(entry, gof.Type))
        if len(inputs) != len(input_types):
            raise IndexError("Not enough inputs to fill in the Subtensor template.", inputs, idx_list)
        for input, expected_type in zip(inputs, input_types):
            if input.type != expected_type:
                raise TypeError("Wrong type for the Subtensor template. Expected %s, got %s." % (input.type, expected_type))

        return gof.Apply(self,
                         (x, y) + inputs,
                         [x.type()])

    def perform(self, node, inputs, (out, )):
        x, y = inputs[:2]
        indices = list(reversed(inputs[2:]))

        def convert(entry):
            if isinstance(entry, gof.Type):
                return indices.pop()
            elif isinstance(entry, slice):
                return slice(convert(entry.start),
                             convert(entry.stop),
                             convert(entry.step))
            else:
                return entry

        cdata = tuple(map(convert, self.idx_list))
        if len(cdata) == 1:
            cdata = cdata[0]
        if not self.inplace:
            x = x.copy()
        sub_x = x.__getitem__(cdata)
        if sub_x.shape:
            # we've sliced out an N-D tensor with N > 0
            if not self.set_instead_of_inc:
                sub_x += y
            else:
                #sub_x += -sub_x + y
                x.__setitem__(cdata, y)
        else:
            # scalar case
            if not self.set_instead_of_inc:
                x.__setitem__(cdata, sub_x + y)
            else:
                x.__setitem__(cdata, y)
        out[0] = x

def split(x, splits_size, n_splits, axis=0):
    the_split = Split(n_splits)
    return the_split(x, axis, splits_size)

class Split(Op):
    """Partition a `TensorVariable` along some axis.

    .. python::
        
        x = vector()
        splits = lvector()
        # you have to declare right away how many split_points there will be.
        ra, rb, rc = split(x, splits, n_splits = 3, axis = 0)  

        f = function([x, splits], [ra, rb, rc])

        a, b, c = f([0,1,2,3,4,5,6], [3, 2, 1])

        #a == [0,1,2]
        #b == [3, 4]
        #c == [5]

    """

    len_splits = None
    """A Split instance will have this many outputs, and require that the splits argument to
    `perform` have exactly this many elements.
    """

    def __init__(self, len_splits):
        self.len_splits = int(len_splits)

    def __eq__(self, other):
        return (type(self) == type(other) and
                self.len_splits == other.len_splits)

    def __hash__(self):
        return hash(Split) ^ self.len_splits

    def __call__(self, *inputs, **kwargs):
        """Override Op.__call__ to suppress unpacking of output list

        """
        node = self.make_node(*inputs, **kwargs)
        node.tag.trace = traceback.extract_stack()[:-1]
        return node.outputs
 
    def make_node(self, x, axis, splits):
        """WRITEME"""
        x = as_tensor_variable(x)
        axis = as_tensor_variable(axis)
        splits = as_tensor_variable(splits)

        if splits.type not in int_vector_types: 
            raise TypeError('splits must have type tensor.lvector', splits.type)
        if axis.type not in int_types: 
            raise TypeError('axis must have type lscalar', axis.type)

#         # The following lines are necessary if we allow splits of zero
#         if isinstance(axis, gof.Constant):
#             x = unbroadcast(x, int(axis.data))
#         else:
#             x = unbroadcast(x, *range(x.type.ndim))

        inputs = [x, axis, splits]
        outputs = [x.type() for i in xrange(self.len_splits)]

        return Apply(self, inputs, outputs)


    def perform(self, node, (x, axis, splits), outputs):
        """WRITEME"""
        #in python 2.4, x.shape[numpy.asarray(1)] don't work.
        if sys.version_info[0:2]==(2, 4) and axis.size==1:
          axis=int(axis)
        
        try:
            len_along_axis = x.shape[axis]
        except :
            raise ValueError('Split.perform() with axis=(%s) is invalid for x.shape==(%s)'
                    %(axis, x.shape))
        if len(splits) != self.len_splits:
            raise ValueError('In Split.perform(), len(splits) != len_splits.', 
                    (len(splits), self.len_splits))

        if numpy.sum(splits) != len_along_axis:
            raise ValueError('The splits sum to %s, expected %s' % (numpy.sum(splits), len_along_axis))
        if not all(splits):
            raise ValueError('Cannot have a split of zero.')
         
        # Checking is done, let's roll the splitting algorithm!
        # Basically we step along the given axis of x, extracting subtensors of size splits[i]
        # as we go along.

        general_key = [slice(None, None, None) for s in x.shape]
        lower_idx = 0
        for i in xrange(self.len_splits):
            upper_idx = lower_idx + splits[i]
            general_key[axis] = slice(lower_idx, upper_idx, None)
            outputs[i][0] = x.__getitem__(general_key).copy()
            lower_idx = upper_idx

    def grad(self, (x, axis, splits), g_outputs):
        """Join the gradients along the axis that was used to split x."""
        return [join(axis, *g_outputs), None, None]


class Rebroadcast(Op):
    """
    Change the input's broadcastable fields in
    some predetermined way.
    e.g.: Rebroadcast((0, True), (1, False))(x)
          would make x broadcastable in axis 0
          and not broadcastable in axis 1
    See also the unbroadcast function.

    ..note: work inplace and work for CudaNdarrayType
    """
    view_map = {0: [0]}
    def __init__(self, *axis):
        self.axis = dict(axis)
    def __eq__(self, other):
        return type(self) == type(other) and self.axis == other.axis
    def __hash__(self):
        items = self.axis.items()
        items.sort() #no ambiguity because each item key is unique
        return hash(type(self)) ^ hash(tuple(items))
    def __str__(self):
        if len(self.axis) == 0:
            broadcast_pattern = []
        else:
            broadcast_pattern = ['?' for i in range(1+numpy.max(self.axis.keys()))]
        for k,v in self.axis.iteritems():
            broadcast_pattern[k] = str(int(v))
        return '%s{%s}' % (self.__class__.__name__, ','.join(broadcast_pattern))
    def make_node(self, x):
        t = x.type.__class__(dtype = x.type.dtype,
                       broadcastable = [self.axis.get(i, b)
                                        for i, b in enumerate(x.type.broadcastable)])
        return Apply(self, [x], [t()])
    def perform(self, node, (x, ), (out, )):
        for axis, value in self.axis.iteritems():
            if value and x.shape[axis] != 1:
                raise ValueError('Dimension %s in Rebroadcast\'s input was supposed to be 1 (got %s instead)' % (axis, x.shape[axis]))
        out[0] = x
    def grad(self, (x, ), (gz,)):
        # restore the broadcasting pattern of the input
        return Rebroadcast(*[(axis, x.type.broadcastable[axis]) for axis, value in self.axis.iteritems()])(gz),

def addbroadcast(x, *axes):
    """
    Make the input broadcastable in the specified axes.
    """
    return Rebroadcast(*[(axis, True) for axis in axes])(x)

def unbroadcast(x, *axes):
    """
    Make the input impossible to broadcast in the specified axes.
    """
    return Rebroadcast(*[(axis, False) for axis in axes])(x)



class Join(Op):
    """
    Concatenate multiple `TensorVariable`s along some axis.

    The axis must be given as first argument. All tensors must have the same
    shape along all dimensions other than this axis.
    Of course, TensorVariable instances do not have a shape, so this error
    cannot be caught until runtime.  See `perform()`.

    For joins involving scalar values, see @stack.

    .. python::
        
        x, y, z = tensor.matrix(), tensor.matrix(), tensor.matrix()
        u = tensor.vector()

        r = join(0, x, y, z)
        c = join(1, x, y, z)
        join(2, x, y, z)    # WRONG: the axis has to be an index into the shape
        join(0, x, u)       # WRONG: joined tensors must have the same rank
    """
    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return hash(type(self))

    def make_node(self, *axis_and_tensors):
        """
        :param axis: an Int or integer-valued Variable

        :param tensors: a variable number (but not zero) of tensors to concatenate along the
        specified axis.  These tensors must have the same shape along all dimensions other than this axis.

        :returns: a symbolic Variable.  It has the same ndim as the input tensors, and the most
        inclusive dtype.

        """
        axis, tensors = axis_and_tensors[0], axis_and_tensors[1:]
        if not tensors:
            raise ValueError('Cannot join an empty list of tensors')
        as_tensor_variable_args= [as_tensor_variable(x) for x in tensors]
        dtypes = [x.type.dtype for x in as_tensor_variable_args]
        out_dtype = scal.upcast(*dtypes)

        if not all(targs.type.ndim for targs in as_tensor_variable_args):
            raise TypeError('Join cannot handle arguments of dimension 0. For joining scalar values, see @stack');

        # When the axis may vary, no dimension can be guaranteed to be
        # broadcastable.
        bcastable = [False] * len(as_tensor_variable_args[0].type.broadcastable)

        # When the axis is fixed, the broadcastable dimensions remain, except
        # for the axis dimension.
        # All concatenated elements must also have the same broadcastable
        # dimensions.
        orig = as_tensor_variable_args
        if isinstance(axis, int):
            bcasts = [x.type.broadcastable[0:axis] + \
                      x.type.broadcastable[axis + 1:] for x in as_tensor_variable_args]
            if not all([bcasts[0] == bc for bc in bcasts[1:]]):
                raise ValueError('Dimensions other than the given axis must'
                    ' match', tensors)
            bcastable[:] = as_tensor_variable_args[0].type.broadcastable
            try:
                bcastable[axis] = False
            except IndexError, e:
                raise ValueError('Join argument "axis" is out of range (given input dimensions)')
            as_tensor_variable_args = [unbroadcast(x, axis) for x in as_tensor_variable_args]
        else:
            as_tensor_variable_args = [unbroadcast(x, *range(x.type.ndim)) for x in as_tensor_variable_args]

        inputs = [as_tensor_variable(axis)] + as_tensor_variable_args
        if inputs[0].type not in int_types: 
            raise TypeError('Axis could not be cast to an integer type', axis, inputs[0].type, int_types)

        outputs = [tensor(dtype = out_dtype,
                          broadcastable = bcastable)]
        node = Apply(self, inputs, outputs)
        if any(not x.type.broadcastable[0] for x in orig):
          node.tag.shape_zero = None
        else:
          node.tag.shape_zero = len(orig)
        #backport node.tag.shape_zero = None if any(not x.type.broadcastable[0] for x in orig) else len(orig)
        return node

    def perform(self, node, axis_and_tensors, (out, )):
        axis, tensors = axis_and_tensors[0], axis_and_tensors[1:]
        out[0] = theano._asarray(numpy.concatenate(tensors, axis = axis),
                dtype=node.outputs[0].type.dtype)

    def grad(self, axis_and_tensors, (gz,)):
        """ The gradient wrt a join op is a `Split`, used to partition the gradient along the
        `axis` which was used for joining.
        """
        axis, tensors = axis_and_tensors[0], axis_and_tensors[1:]
        if 'float' in tensors[0].dtype or 'complex' in tensors[0].dtype:
            # assume that this is differentiable
            split = Split(len(tensors))
            split_gz = split(gz, axis, stack(*[shape(x)[axis] for x in tensors]))
            return [None] + split_gz
        else:
            # assume that this isn't differentiable
            return [None] * (1 + len(tensors)) 

    def _native_grad(self, axis_and_tensors, (gz,)):
        """WRITEME"""
        axis, tensors = axis_and_tensors[0], axis_and_tensors[1:]
        sizes_along_axis = [shape(x)[axis] for x in tensors]
        n_dims = len(shape(tensors[0]))
        idx = [0]
        for s in sizes_along_axis:
            idx.append(idx[-1] + s)
        # The gradient w.r.t. the k-th tensor is a slice of gz along the
        # 'axis' dimension.
        return [gz[[slice(None)] * axis + [slice(idx[k], idx[k + 1])] + \
                [slice(None)] * (n_dims - axis - 1)] \
                for k in range(len(sizes_along_axis))]

    def vec_length(self, node):
        """Guess the length of a Join Variable"""
        assert isinstance(node.owner.op, Join)
        if node.ndim != 1:
            raise TypeError('argument must be symbolic vector')
        if node.owner.tag.shape_zero is None:
          raise ValueError("could not determine vector length")
        else:
          return node.owner.tag.shape_zero

@_redefine_asRoutine(Join())
def join(axis, *tensors):
    """
    Convenience function to concatenate `TensorType`s along the given axis.

    :Parameters:
     - `tensors` : list of tensors (or list-like)
       A list of tensors to be concatenated along the given axis.
     - `axis` : int (symbolic or literal)
       On which dimension should the tensors be joined?  The `axis` must be a valid index into
       the shape of the tensors to be concatenated.
       The `axis` parameter may either be an integer or an object that can be converted to a
       scalar using `as_scalar`(`axis`). In the former case, the axis is fixed at construction,
       while in the latter it may vary over time depending on the value of the `axis` variable.

    The shapes of the tensors to be concatenated must be all identical, except in the dimension
    (`axis`) on which they are to be joined.

    """

pprint.assign(lambda pstate, r: r.owner and isinstance(r.owner.op, Join),
              printing.FunctionPrinter('join'))



@constructor
def shape_padleft(t, n_ones=1):
    """Reshape `t` by left-padding the shape with `n_ones` 1s
    
    See also: `shape_padright` and `Dimshuffle`
    """
    _t = as_tensor_variable(t)

    pattern = ['x']*n_ones + [i for i in range(_t.type.ndim)]
    return DimShuffle(_t.broadcastable, pattern)(_t)

@constructor
def shape_padright(t, n_ones=1):
    """Reshape `t` by right-padding the shape with `n_ones` 1s
    
    See also: `shape_padleft` and `Dimshuffle`
    """
    _t = as_tensor_variable(t)

    pattern = [i for i in range(_t.type.ndim)] + ['x']*n_ones
    return DimShuffle(_t.broadcastable, pattern)(_t)

@constructor
def stack(*tensors):
    """Insert the arguments as slices into a tensor of 1 rank greater.
    EXAMPLE
    """
    # If all tensors are scalars of the same type, call make_vector.
    # It makes the graph simpler, by not adding DimShuffles and Rebroadcasts
    if numpy.all([isinstance(t, Variable) and\
                  isinstance(t.type, TensorType) and\
                  t.ndim==0 and t.type==tensors[0].type\
                  for t in tensors]):
        return theano.tensor.opt.make_vector(*tensors)
    return join(0, *[shape_padleft(t, 1) for t in tensors])

@constructor
def concatenate(tensor_list, axis=0):
    """Alias for `join`(axis, *tensor_list).
    
    This function is similar to `join`, but uses the signature of numpy's concatenate function.

    This function 
    :Exceptions:
     - `TypeError` : the tensor_list must be a tuple or list

    """
    # Check someone did not make the common mistake to do something like:
    #   c = concatenate(x, y)
    # instead of
    #   c = concatenate((x, y))
    if not isinstance(tensor_list, (tuple, list)):
        raise TypeError("The 'tensors' argument must be either a tuple "
                "or a list, make sure you did not forget () or [] around "
                "arguments of concatenate.", tensor_list)
    return join(axis, *tensor_list)

def get_vector_length(v):
    """Return the run-time length of a symbolic vector.

    :Parameters:
     - `v` : A rank-1 TensorType variable.

    :Exceptions:
     - `TypeError` : `v` hasn't the proper type.
     - `ValueError` : No special case applies, the length is not known.
    
    In general this is not possible, but for a number of special cases the length can be
    determined at compile / graph-construction time.  This function implements these special
    cases.

    """
    v = as_tensor_variable(v)
    if v.ndim != 1:
        raise TypeError('argument must be symbolic vector')
    if isinstance(v, gof.Constant) and v.type.ndim == 1:
        return len(v.data)
    if v.owner and isinstance(v.owner.op, Join):
        try:
            return join.vec_length(v)
        except ValueError:
            pass
    if v.owner and isinstance(v.owner.op, theano.tensor.opt.MakeVector):
        return len(v.owner.inputs)
    if v.owner and isinstance(v.owner.op, Shape):
        return v.owner.inputs[0].type.ndim
    raise ValueError("length not known")

@constructor
def horizontal_stack(*args):
    """
    Horizontally stack two L{TensorType}s.
    Stack two L{TensorType}s along the second axis (column wise). These
    L{TensorType}s must have the same shape along all dimensions but the
    second.
    """
    # Note: 'horizontal_stack' and 'vertical_stack' do not behave exactly like
    # Numpy's hstack and vstack functions. This is intended, because Numpy's
    # functions have potentially confusing/incoherent behavior (try them on 1D
    # arrays). If this is fixed in a future version of Numpy, it may be worth
    # trying to get closer to Numpy's way of doing things. In the meantime,
    # better keep different names to emphasize the implementation divergences.
    assert len(args) >= 2
    for arg in args: assert arg.type.ndim == 2
    return concatenate(args, axis=1)

@constructor
def vertical_stack(*args):
    assert len(args) >= 2
    for arg in args: assert arg.type.ndim == 2
    return concatenate(args, axis=0)

if 0: #vertical and horizontal stacking are deprecated.  Better to use stack() and join().
    class VerticalStack(Op):
        """
        Vertically stack two L{TensorType}s.
        Stack two L{TensorType}s along the first axis (row wise). These
        L{TensorType}s must have the same shape along all dimensions but the
        first.

        @attention: Because we use vstack as the implementation, if the
        inputs have 1-dimension, the output will have 2-dimensions.
        """
        def make_node(self, x, y):
            x = as_tensor_variable(x)
            y = as_tensor_variable(y)
            assert x.type.dtype == y.type.dtype
            if x.type.broadcastable[1:] != y.type.broadcastable[1:]:
                raise NotImplementedError
            inputs = [x, y]
            bcastable = (False, ) + x.type.broadcastable[1:]
            outputs = [tensor(dtype = x.type.dtype,
                              broadcastable = bcastable)]
            return Apply(self, inputs, outputs)
        def perform(self, node, (x, y), (out, )):
            assert x.ndim == y.ndim
            # Make sure every dimension (save the first) is the same
            for i in range(x.ndim): assert i == 0 or x.shape[i] == y.shape[i]
            out[0] = numpy.vstack([x, y])
        def grad(self, (x, y), (gz,)):
            """
            @todo: Make VSplit (or this grad implementation) its own L{Op},
            that way we can do more sanity-checking::
                assert x.ndim == y.ndim
                # Make sure every dimension (save the first) is the same
                for i in range(x.data.ndim): assert i == 0 or x.data.shape[i] == y.shape[i]
                etc...
            """
            xs = shape(x)
            ys = shape(y)
            return gz[:xs[0]], gz[xs[0]:]
    vertical_stack = VerticalStack()

else:
    pass


class Reshape(Op):
    """Perform a reshape operation of the input x to the new shape shp.
    The number of dimensions to which to reshape to (ndim) must be known at graph 
    build time."""
    view_map = {0: [0]} #output 0 is potentially aliased to inputs [0]
    def __init__(self, ndim, name = None):
        self.ndim = ndim
        self.name = name

    def __eq__(self, other):
        # .name does not participate because it doesn't affect computations
        return (type(other) is type(self)) and (other.ndim == self.ndim)
    def __hash__(self):
        # .name does not participate because it doesn't affect computations
        return hash(type(self)) ^ hash(self.ndim)
    def __str__(self):
        return '%s{%s}' %(self.__class__.__name__, self.ndim)
    def make_node(self, x, shp):
        x = as_tensor_variable(x)
        shp = as_tensor_variable(shp, ndim=1)
        if not shp.dtype.startswith('int'):
            raise TypeError("Shape must be integers")
        assert shp.ndim == 1
        if isinstance(shp, TensorConstant):
            bcast = [s==1 for s in shp.data]
            return gof.Apply(self, [x, shp], [tensor(x.type.dtype, bcast)])
        else:
            return gof.Apply(self, [x, shp], [tensor(x.type.dtype, [False]*self.ndim)])
    def perform(self, node, (x, shp), (out,)):
        if (len(shp) != self.ndim):
            raise ValueError('shape argument to Reshape.perform has incorrect length %i'
                    ', should be %i' % (len(shp), self.ndim), shp)
        try:
            out[0] = numpy.reshape(x, shp)
        except:
            raise ValueError('Cannot reshape input of shape %s to shape %s' % (x.shape,shp))
    def grad(self, (x, shp), (g_out,)):
        return [reshape(g_out, shape(x), ndim=x.ndim), None]

def reshape(x, newshape, ndim=None, name=None):
    if ndim is None:
        ndim = get_vector_length(newshape)
    op = Reshape(ndim, name)
    rval = op(x, newshape)

    if  isinstance(newshape, (list, tuple)):
        rval.tag.shape = newshape
    return rval

class Flatten(Op):
    """Flattens a tensor to `outdim` dimensions by preserving the leading outdim-1 shape
    components.
    """
    view_map = {0:[0]}
    def __init__(self, outdim=1):
        self.outdim = int(outdim)
    def __eq__(self, other):
        return type(self) == type(other) and self.outdim == other.outdim
    def __hash__(self):
        return hashtype(self)^hash(self.outdim)
    def make_node(self, x):
        t_x = as_tensor_variable(x)
        if self.outdim < 1 or (x.ndim and self.outdim > x.ndim):
            raise ValueError('invalid output ndimensions(%i) for tensor of rank %i' %(self.outdim, t_x.ndim))
        return gof.Apply(self, [t_x], [tensor(x.type.dtype, (False,)*self.outdim)])
    def perform(self, node, (x,), (out,)):
        outdim = self.outdim
        if outdim == 1:
            out[0] = x.reshape(x.size)
        elif outdim == len(x.shape):
            out[0] = x
        else:
            newshape = x.shape[:outdim-1] + (numpy.prod(x.shape[outdim-1:]),)
            #print 'newshape', newshape, x.shape, x.shape
            out[0] = x.reshape(newshape)
    def grad(self, (x,), (g_out,)):
        return [reshape(g_out, shape(x), x.ndim)]

def flatten(x, outdim=1): 
    return Flatten(outdim)(x)

class TileGrad(Op):
    """Calculates the gradient of the Tile Op"""
    #this is so weird, I can't think of how to make this a general thing.
    def make_node(self, x, reps, g_out):
        return gof.Apply(self, [x, reps, g_out], [x.type()])
    def perform(self, node, (x, reps, g_out), (gx,)):
        xsh = x.shape
        if len(reps)==2 and reps[1] == 1 and len(x.shape) == 1:
            gx[0] = numpy.sum(g_out, axis=0)
        else:
            raise NotImplementedError('x.shape, reps combination not supported',
                    (x.shape, reps))
tilegrad = TileGrad()


class Tile(Op):
    """Tiles its input according to reps. Reps is of same dimension as x
    and contains the number of times to tile x in each dimension"""
    def __init__(self, ndim):
        self.ndim = ndim
    def __eq__(self, other):
        return (type(other) is Tile) and (other.ndim == self.ndim)
    def __hash__(self):
        return hash(Tile) ^ hash(self.ndim)

    def make_node(self, x, reps):
        x = as_tensor_variable(x)
        reps = as_tensor_variable(reps)
        return gof.Apply(self, [x, reps], [tensor(x.type.dtype, [False,] * self.ndim)])
    def perform(self, node, (x, reps), (out,)):
        out[0] = numpy.tile(x, reps)
        if len(out[0].shape) != self.ndim:
            raise ValueError('Tile.perform produced incorrect shape')
    def grad(self, (x, reps), (g_out,)):
        return [tilegrad(x, reps, g_out), None]

def tile(x, reps, ndim=None):
    if not hasattr(tile, 'op'):
        tile.op = {}
    if ndim is None:
      ndim = len(reps)

    #backport
    #ndim = len(reps) if ndim is None else ndim #not sure if len(shp) is going to work.
    if ndim not in tile.op:
        tile.op[ndim] = Tile(ndim)
    return tile.op[ndim](x, reps)


class ARange(Op):
    """Create an array containing evenly spaced values within a given interval.

    Parameters and behaviour are the same as numpy.arange().
    """

    def __init__(self, dtype):
        self.dtype = dtype

    def __eq__(self, other):
        return type(self) == type(other) and self.dtype == other.dtype

    def __hash__(self):
        return hash(self.dtype)

    def __str__(self):
        return self.__class__.__name__

    def make_node(self, start, stop, step):
        start, stop, step = map(as_tensor_variable, (start, stop, step))
        assert start.ndim == 0
        assert stop.ndim == 0
        assert step.ndim == 0

        inputs = [start, stop, step]
        outputs = [tensor(self.dtype, (False,))]
        return Apply(self, inputs, outputs)

    def infer_shape(self, node, i_shapes):
        start, stop, step = node.inputs
        def is_constant_value(var, value):
            try:
                v = get_constant_value(var)
                return numpy.all(v == value)
            except:
                pass
            return False

        if is_constant_value(step, 1):
            if is_constant_value(start, 0):
                return [(cast(stop, 'int64'),)]
            else:
                return [(theano.tensor.max([cast(stop-start, 'int64'),0]),)]
        else:
            return [(theano.tensor.max([cast(ceil(cast((stop-start),'float64')/step),'int64'),0]),)]

    def perform(self, node, (start, stop, step), (out,)):
        start = start.item()
        stop = stop.item()
        step = step.item()
        out[0] = numpy.arange(start, stop, step, dtype=self.dtype)

    def grad(self, inputs, (gz,)):
        return [None] * len(inputs)

_arange = {}
def arange(start, stop=None, step=1, dtype=None):
    # If only one argument is provided, it is in fact the "stop" argument,
    # and start is 0.
    if stop is None:
        start, stop = 0, start

    start, stop, step = map(as_tensor_variable, (start, stop, step))
    # If dtype is not provided, infer it from the other arguments
    if dtype is None:
        dtype = scal.upcast(start.type.dtype, stop.type.dtype, step.type.dtype)

    if dtype not in _arange:
        _arange[dtype] = ARange(dtype)
    return _arange[dtype](start, stop, step)


class PermuteRowElements(Op):
    """Permute the elements of each row (inner-most dim) of a tensor.

    A permutation will be applied to every row (vector) of the input tensor x.
    Depending on the dimensionality of x and the permutation tensor y,
    different cases are possible.
    If y.ndim = 1, y is a single permutation, that will be applied to every
    vector of x. For instance, if x is a matrix, the same permutation will be
    applied to each row of x.
    If x.ndim = y.ndim, each row of x corresponds to a row of y, containing
    a permutation that will be applied to that row. For instance, if x and y
    are two matrices, a different permutation will be applied to each row of x.
    If x.ndim > y.ndim, y will be broadcasted to fit x, then each row (vector)
    of x will be reordered according to the corresponding row of y. (This is
    a generalization of the first case).
    If x.ndim = 1, every permutation in y will be applied to x, and the output
    will contain all the results.
    If x.ndim < y.ndim, x will be broadcasted to fit y, and different
    permutations contained in y will be applied to each vector in x. (This is
    a generalization of the previous case).

    If the "inverse" argument is True, the Op will perform the inverse
    permutation instead.
    """

    def make_node(self, x, y, inverse):
        x = as_tensor_variable(x)
        y = as_tensor_variable(y)
        inverse = as_tensor_variable(inverse)

        # y should contain integers
        assert y.type.dtype.startswith('int') or y.type.dtype.startswith('uint')
        # Inverse should be an integer scalar
        assert inverse.type.ndim == 0 and\
                (inverse.type.dtype.startswith('int') or\
                 inverse.type.dtype.startswith('uint'))

        # Match shapes of x and y
        x_dim = x.type.ndim
        y_dim = y.type.ndim

        if x_dim > y_dim:
            y = shape_padleft(y, n_ones=(x_dim - y_dim))
        elif x_dim < y_dim:
            x = shape_padleft(x, n_ones=(y_dim - x_dim))

        # Compute the broadcastable pattern of the output
        out_broadcastable = [xb and yb for xb, yb in zip(x.type.broadcastable, y.type.broadcastable)]
        out_type = tensor(dtype = x.type.dtype, broadcastable = out_broadcastable)

        inputlist = [x, y, inverse]
        outputlist = [out_type]
        return Apply(self, inputlist, outputlist)

    def _rec_perform(self, node, x, y, inverse, out, curdim):
        """Perform the permutation by doing a recursion over the input dimensions.

        For every dimension, starting with the leftmost, the right set of
        indices is determined (depending if broadcasting or not), then
        the function is recursively called on the appropriate subtensors.

        The terminal case is reached when the current tensors are vector,
        then the permutation contained in y is applied to x.

        :param x: The input tensor, on which the permutation is applied
        :param y: Tensor containing the permutations to apply
        :param out: Tensor storing the output result
        :param curdim: Counter of the current depth of recursion
        :param inverse: Wether to apply permutations or their inverse
        """
        if len(x.shape) == 1:
            # Numpy advanced indexing works in this case
            if inverse:
                out[y] = x[:]
            else:
                out[:] = x[y]
        else:
            xs0 = x.shape[0]
            ys0 = y.shape[0]
            if xs0 == ys0:
                for i in range(xs0):
                    self._rec_perform(node, x[i], y[i], inverse, out[i], curdim+1)
            elif ys0 == 1 and node.inputs[1].type.broadcastable[curdim]:
                # Broadcast y
                for i in range(xs0):
                    self._rec_perform(node, x[i], y[0], inverse, out[i], curdim+1)
            elif xs0 == 1 and node.inputs[0].type.broadcastable[curdim]:
                # Broadcast x
                for i in range(ys0):
                    self._rec_perform(node, x[0], y[i], inverse, out[i], curdim+1)
            else:
                raise ValueError('Dimension mismatch: %s, %s' % (xs0, ys0))

    def perform(self, node, (x, y, inverse), (outs,)):
        x_s = x.shape
        y_s = y.shape
        assert len(x_s) == len(y_s)

        # Make sure the output is big enough
        out_s = []
        for xdim, ydim in zip(x_s, y_s):
            if xdim == ydim:
                outdim = xdim
            elif xdim == 1:
                outdim = ydim
            elif ydim == 1:
                outdim = xdim
            else:
                raise ValueError('Dimension mismatch: %s, %s' % (xdim, ydim))
            out_s.append(outdim)

        if outs[0] is None or outs[0].shape != out_s:
            outs[0] = numpy.empty(out_s, dtype=x.dtype)

        self._rec_perform(node, x, y, inverse, outs[0], curdim=0)

    def grad(self, (x, y, inverse), (gz,)):
        # First, compute the gradient wrt the broadcasted x.
        # If 'inverse' is False (0), apply the inverse of y on gz.
        # Else, apply y on gz.
        gx = permute_row_elements(gz, y, eq(inverse, 0))

        # If x has been broadcasted along some axes, we need to sum
        # the gradient over these axes, but keep the dimension (as
        # broadcastable)
        broadcasted_dims = [dim for dim in range(gz.type.ndim)\
                if x.type.broadcastable[dim] and not gz.type.broadcastable[dim]]
        gx = Sum(axis = broadcasted_dims)(gx)

        # Sum(...) removed the dimensions in broadcasted_dims,
        # so we need to put them back.
        newdims = []
        i = 0
        for dim in range(gz.type.ndim):
            if dim in broadcasted_dims:
                newdims.append('x')
            else:
                newdims.append(i)
                i += 1

        gx = DimShuffle(gx.type.broadcastable, newdims)(gx)
        assert gx.type.broadcastable == x.type.broadcastable
        return [gx, None, None]

_permute_row_elements = PermuteRowElements()
def permute_row_elements(x, y, inverse=0):
    return _permute_row_elements(x, y, inverse)

def inverse_permutation(perm):
    """Computes the inverse of permutations.
    Each row of input should contain a permutation of the first integers.
    """
    return permute_row_elements(arange(perm.shape[-1]), perm, inverse=True)

#########################
# Advanced indexing
#########################
#
# Should reproduce numpy's behaviour:
# http://docs.scipy.org/doc/numpy/reference/arrays.indexing.html#advanced-indexing

AddConfigVar('experimental.advanced_indexing',
        "enable not-well-tested advanced indexing functionality",
        BoolParam(False))


class AdvancedSubtensor1(Op):
    """Implement x[ilist] where ilist is a vector of integers."""

    def __hash__(self):
        return hash(type(self))
    def __eq__(self, other):
        type(self) == type(other)

    def make_node(self, x, ilist):
        x_ = as_tensor_variable(x)
        ilist_ = as_tensor_variable(ilist)
        if ilist_.type.dtype[:3] not in ('int', 'uin'):
            raise TypeError('index must be integers')
        if ilist_.type.broadcastable != (False,):
            raise TypeError('index must be vector')
        if x_.type.ndim == 0:
            raise TypeError('cannot index into a scalar')
        if x_.type.broadcastable[0]:
            # the caller should have made a copy of x len(ilist) times
            raise TypeError('cannot index into a broadcastable dimension')

        return Apply(self, [x_, ilist_], [x_.type()])

    def perform(self, node, (x,i), (out,)):
        out[0] = x[i]

    def grad(self, inputs, (gz,)):
        class NotImplementedOp(Op):
            # This op should be pruned from the graph.
            # This Op can be created in a graph,
            # but it will cause problems if one of your parameters actually depends on it!
            def make_node(self, *args):
                return Apply(self, args, [inputs[0].type()])
        return [NotImplementedOp()(gz)]+[None]*(len(inputs)-1)


class AdvancedSubtensor(Op):
    """Return a subtensor copy, using advanced indexing.
    """
    # Should be used by __getitem__ and __getslice__, as follow:
    # AdvancedSubtensor(args)(self, *args),
    # if args contains and advanced indexing pattern

    def __init__(self, args): #idx_list?
        # For the moment, __init__ will be passed the whole list of arguments
        #TODO: see what's the best solution
        self.args = args #?

        #FIXME: do not store variables in the class instance

        #FIXME
        #if len(args) != 2:
        #    print >>sys.stderr, 'WARNING: Advanced indexing with %i arguments not supported yet' % len(args)
        #    print >>sys.stderr, '  arguments are:', args

    def make_node(self, x, *inputs):
        x = as_tensor_variable(x)
        #FIXME
        if x.ndim == 2 and len(inputs) == 2:
            ind1 = as_tensor_variable(inputs[0])
            ind2 = as_tensor_variable(inputs[1])
            if not (ind1.type.dtype.startswith('int') or ind1.type.dtype.startswith('uint')):
                raise TypeError('the indices into a matrix must be int or uint. It is ',ind1.type.dtype)
            if not (ind2.type.dtype.startswith('int') or ind2.type.dtype.startswith('uint')):
                raise TypeError('the indices into a matrix must be int or uint. It is ',ind2.type.dtype)

            if ind1.ndim == 1 and ind2.ndim == 1:
                return gof.Apply(self,
                        (x,) + inputs,
                        [tensor(dtype = x.type.dtype,
                            broadcastable = [False])])
            raise NotImplementedError('Advanced indexing of x (of dimension %i) with these argument dimensions (%s) not supported yet'\
                    % (x.ndim, ','.join(str(input.ndim) for input in inputs)))
        raise NotImplementedError('Advanced indexing of x with arguments (%s) not supported yet'\
                % ','.join(str(input) for input in inputs))

    def infer_shape(self, node, ishapes):
        # Really special case
        if len(ishapes) == 3:
            xshp, ind1shp, ind2shp = ishapes
            if len(xshp) == 2 and len(ind1shp) == 1 and len(ind2shp) == 1:
                # if the graph is correct, we can assume ind1shp[0] and
                # ind2shp[0] will have the same value.
                # Try to return the one closest to the graph input.
                if node.inputs[2].owner is None:
                    return [ind2shp]
                else:
                    return [ind1shp]
        # Default case, we don't know
        return node.env.shape_feature.default_infer_shape(node, ishapes)

    def perform(self, node, inputs, (out,)):
        # TODO: in general, we need to re-pack the inputs into a valid index, just like
        # subtensor
        out[0] = inputs[0].__getitem__(inputs[1:])
        #return 
        #raise NotImplementedError()

    def grad(self, inputs, (gz,)):
        x = inputs[0]
        rest = inputs[1:]
        return [AdvancedIncSubtensor(self.args)(zeros_like(x), gz, *rest)] + [None]*len(rest)

class AdvancedIncSubtensor(Op):
    """Increments a subtensor using advanced indexing.
    """

    def __init__(self, args): #idx_list? inplace=False?
        self.args = args

    def make_node(self, x, y, *inputs):
        x = as_tensor_variable(x)
        y = as_tensor_variable(y)

        if x.ndim == 2 and y.ndim == 1 and len(inputs) == 2:
            ind1 = as_tensor_variable(inputs[0])
            ind2 = as_tensor_variable(inputs[1])
            if ind1.ndim == 1 and ind2.ndim == 1:
                return gof.Apply(self,
                        (x, y) + inputs,
                        [tensor(dtype = x.type.dtype,
                            broadcastable = x.type.broadcastable)])
            raise NotImplementedError('Advanced indexing increment of x (of dimension %i) by y (of dimension %i) with these argument dimensions (%s) not supported yet'\
                    % (x.ndim, y.ndim, ','.join(str(input.ndim) for input in inputs)))
        raise NotImplementedError('Advanced indexing increment of x (of dim %i) by y (of dim %i) with arguments (%s) not supported yet'\
                % (x.ndim, y.ndim, ','.join(str(input) for input in inputs)))

    def perform(self, node, inputs, (out,)):
        # TODO: same thing as in AdvancedSubtensor's perform TODO
        out[0] = inputs[0].copy()
        out[0][inputs[2:]] += inputs[1]

    #def grad?
        # grad on x is grad  on output
        # grad on y is grad_output[idx_list]
        # grad on rest is None





#########################
# Linalg : Dot
#########################
#
# For BLAS-related ops see blas.py
#
# TODO: Dotinv should go here, Eigs, Svd, etc.

class Dot(Op):
    """Compute matrix-matrix, matrix-vector products and vector inner-products.

    :note: matrix-matrix products are sometimes optimized to Dot22 ops (see tensor.blas)

    :note: non matrix-matrix products (including matrix-vector products) are handled by numpy.  Ensure that you have linked numpy with a fast BLAS.

    """

    # the rationale for Dot22 is related to getting GEMM Ops into the graph.  See Dot22 in tensor.blas for details.
    
    def make_node(self, *inputs):
        inputs = map(as_tensor_variable, inputs)

        numpy_semantics = 0
        if numpy_semantics:
            #numpy defines dot for tensor pairs with any rank
            if len(inputs) != 2:
                raise TypeError("Wrong number of inputs for %s (got %i, expected 2)" % self)
            i_broadcastables = [input.type.broadcastable for input in inputs]
            bx, by = i_broadcastables
            if len(bx) == 0:     # x is a scalar
                bz = by
            else:
                if len(by) >= 2: #y is a matrix or tensor
                    bz = bx[:-1] + by[:-2] + by[-1:]
                elif len(by)==1: #y is vector
                    bz = bx[:-1]
                else:            #y is a scalar
                    bz = bx
        else:
            x, y = inputs
            nx = x.type.ndim
            ny = y.type.ndim

            if nx not in (1,2): raise TypeError('not matrix or vector', x)
            if ny not in (1,2): raise TypeError('not matrix or vector', y)

            if nx == 2 and ny == 2:
                bz = [x.type.broadcastable[0], y.type.broadcastable[1]]
            elif nx == 1 and ny == 2:
                bz = [y.type.broadcastable[1]]
            elif nx == 2 and ny == 1:
                bz = [x.type.broadcastable[0]]
            else:
                bz = []

        i_dtypes = [input.type.dtype for input in inputs]
        outputs = [tensor(scal.upcast(*i_dtypes), bz)]
        return Apply(self, inputs, outputs)

    def perform(self, node, (x, y), (z, )):
        try:
            # the asarray is here because dot between two vectors gives a numpy float object
            # but we need to return a 0d ndarray
            z[0] = numpy.asarray(numpy.dot(x, y))
        except ValueError, e:
            # The error raised by numpy has no shape information, we mean to add that
            e.args = e.args + (x.shape, y.shape)
            raise

    def grad(self, (x, y), (gz,)):
        if gz.type.ndim == 0:
            rval = gz * y, gz * x
        elif x.type.ndim == 1 and y.type.ndim > 1:
            rval = dot(gz, y.T), outer(x.T, gz)
        elif x.type.ndim > 1 and y.type.ndim == 1:
            rval = outer(gz, y.T), dot(x.T, gz) 
        else:
            rval = dot(gz, y.T), dot(x.T, gz)
        return cast(rval[0], x.dtype), cast(rval[1], y.dtype)

    def infer_shape(self, node, (xshp,yshp)):
        x, y = node.inputs
        if x.ndim == 2 and y.ndim == 2:
            return [(xshp[0], yshp[1])]
        if x.ndim == 1 and y.ndim == 2:
            return [(yshp[1],)]
        if x.ndim == 2 and y.ndim == 1:
            return [(xshp[0],)]
        if x.ndim == 1 and y.ndim == 1:
            return [()]
        raise NotImplementedError()

    def __str__(self):
        return "dot"
dot = Dot()
pprint.assign(dot, printing.OperatorPrinter(printing.special['middle_dot'], -1, 'left'))

#########################
# Linalg : TensorDot
#########################
class TensorDotGrad(Op):
    def __init__(self, axes):
        self.axes = axes;

    def __eq__(self, other):
        return type(self) == type(other) and self.axes == other.axes

    def __hash__(self):
        return hashtype(self) ^ hash(self.axes) ^ 89234

    def make_node(self, x, y, gz):
        assert isinstance(x, Variable)
        assert isinstance(y, Variable)
        assert isinstance(gz, Variable)
        gx = x.type()
        gy = y.type()
        return Apply(self, [x,y,gz], [gx, gy])

    def perform(self, node, (x, y, gz), (gx,gy)):

        sum_over_y = range(y.ndim)
        [sum_over_y.remove(q) for q in self.axes[1]]
        sum_over_x = range(x.ndim)
        [sum_over_x.remove(q) for q in self.axes[0]]

        _gx = numpy.tensordot(gz, y, [range(x.ndim-len(self.axes[0]),gz.ndim), sum_over_y])
        idx = numpy.hstack((sum_over_x, self.axes[0]))
        newshapex = numpy.zeros(x.ndim)
        newshapex[[newpos for newpos in idx]] = [i for i in range(x.ndim)]
        gx[0] = numpy.transpose(_gx, newshapex)
        assert str(gx[0].dtype) == 'float64'

        _gy = numpy.tensordot(x, gz, [sum_over_x, range(x.ndim-len(self.axes[0]))])
        idy = numpy.hstack((self.axes[1], sum_over_y))
        newshapey = numpy.zeros(y.ndim)
        newshapey[[newpos for newpos in idy]] = [i for i in range(y.ndim)]
        gy[0] = numpy.transpose(_gy, newshapey)
        assert str(gy[0].dtype) == 'float64'

tensordot_grad = TensorDotGrad

class TensorDot(Op):
    """Compute tensor-tensor products over the given axes.
    See numpy documentation for details.
    (http://docs.scipy.org/doc/numpy/reference/generated/numpy.tensordot.html)

    """

    def __init__(self, axes):
        self.axes = axes;

    def __eq__(self, other):
        return type(self) == type(other) and self.axes == other.axes

    def __hash__(self):
        return hashtype(self) ^ hash(self.axes) ^ 89234

    def make_node(self, x, y):

        axesdim = numpy.size(self.axes)/2
        x, y = map(as_tensor_variable, [x, y])

        if axesdim > x.type.ndim or axesdim > y.type.ndim:
            raise TypeError('Cannot sum over more dimensions than input. %i > %i,%i' %
                    axesdim, x.type.ndim, y.type.ndim)
       
        outdim = x.type.ndim + y.type.ndim - 2*axesdim
        output = tensor(dtype=x.dtype, broadcastable=[False]*outdim);
        return Apply(self, inputs=[x,y], outputs=[output,])

    def perform(self, node, (x, y), (z,)):
        try:
            z[0] = numpy.asarray(numpy.tensordot(x, y, self.axes))
            assert str(z[0].dtype) == 'float64'
        except ValueError, e:
            # The error raised by numpy has no shape information, we mean to add that
            e.args = e.args + (x.shape, y.shape, self.axes)
            raise

    def grad(self, (x, y), (gz,)):
        gx, gy = tensordot_grad(self.axes)(x, y, gz)
        return [gx, gy]
    
    def __str__(self):
        return "tensordot"
tensordot = TensorDot

#TODO: tensordot should be function as described in rst docs.

class Outer(Op):
    """ Compute vector-vector outer product
    """
    def make_node(self, *inputs):
        inputs = map(as_tensor_variable, inputs)

        x, y = inputs
        nx = x.type.ndim
        ny = y.type.ndim

        if nx != 1: raise TypeError('non-vector arg0 to outer()', x)
        if ny != 1: raise TypeError('not-vector arg1 to outer()', y)
        
        bz = [x.type.broadcastable[0], y.type.broadcastable[0]]

        i_dtypes = [input.type.dtype for input in inputs]
        outputs = [tensor(scal.upcast(*i_dtypes), bz)]
        return Apply(self, inputs, outputs)

    def perform(self, node, (x, y), (z, )):
        z[0] = numpy.outer(x, y)
    def grad(self, (x, y), (gz,)):
        return dot(gz, y), dot(x, gz) #no transposing necessary
    def __str__(self):
        return "outer"
outer = Outer()


#########################
# Gradient
#########################

def grad(cost, wrt, g_cost=None, consider_constant=[], warn_type=False):
    """
    :type cost: `Variable`
    :type wrt: `Variable` or list of `Variable`s.
    :type g_cost: `Variable` broadcastable to size of `cost`, or None
    :param g_cost: an expression for the gradient through cost.  The default is
        ``ones_like(cost)``.
    :param consider_constant: a list of expressions not to backpropagate through

    :param warn_type: a value of True will cause warnings to be logged for any Op that emits a
        gradient that does not match its input type.

    :rtype: `Variable` or list of `Variable`s (depending upon `wrt`)

    :return: symbolic expression of gradient of `cost` with respect to `wrt`.
    If `wrt` is a list, then return a list containing the gradient of `cost` wrt
    each element of the list.  If an element of `wrt` is not differentiable
    with respect to the output, then a zero variable is returned.

    This function is a wrapper around a the more general function
    `theano.gradient.grad_sources_inputs``.

    """
    if not isinstance(cost, TensorVariable):
        raise TypeError('In tensor.grad(), cost argument should be a TensorVariable.', cost)

    if cost.type.ndim:
        _warn('the passing of a non-scalar cost to theano.tensor.grad() is deprecated.'
                '  Use the lower-level '
                'theano.gradient if you really want to do this')

    if g_cost is None:
        g_cost = ones_like(cost)
    inputs = gof.graph.inputs([cost])
    gmap = gradient.grad_sources_inputs([(cost, g_cost)], inputs + consider_constant,
            warn_type=warn_type)

    # Note that it is important to use `zeros_like` when there is no gradient,
    # instead of returning a scalar constant equal to zero. Otherwise we lose
    # the guarantee that the gradient has same shape as `wrt`.
    if isinstance(wrt, (list, tuple)):
        return [gmap.get(p, zeros_like(p)) for p in wrt]
    else:
        return gmap.get(wrt, zeros_like(wrt))

class numeric_grad:
    """WRITEME"""
    type_eps = {'float64': 1e-7,
            'float32': 3e-3}

    def __init__(self, f, pt, eps=None):
        """Return the gradient of f at pt.
        
        This function computes the gradient by a one-sided finite differences of a
        fixed step size (eps).
        
        It is assumed that f(...) will return a scalar.
        It is assumed that all f's inputs are numpy.ndarray objects.

        :param eps: the stepsize for the finite differencing.  None means input
        dtype-dependent. See `type_eps`.
        """

        def prod(inputs):
            rval = 1
            for i in inputs:
                rval *= i
            return rval

        packed_pt = False
        if not isinstance(pt, (list, tuple)):
            pt = [pt]
            packed_pt = True

        apt = [numpy.array(p) for p in pt]

        shapes = [p.shape for p in apt]
        dtypes = [str(p.dtype) for p in apt]

        # TODO: remove this eventually (why was this here in the first place ?)
        # In the case of CSM, the arguments are a mixture of floats and integers...
        #if not dtypes == [dtypes[0]] * len(apt):
            #raise TypeError('All function arguments must have same dtype')

        total_size = __builtin__.sum(prod(sh) for sh in shapes)

        working_dtype = __builtin__.min((self.type_eps[dt], dt) for dt in dtypes)[1]

        #create un-initialized memory
        x = numpy.ndarray((total_size,), dtype=working_dtype)
        gx = numpy.ndarray((total_size,), dtype=working_dtype)

        if eps is None:
            eps = __builtin__.max(self.type_eps[dt] for dt in dtypes)


        #set up aliases so that apt[i] is backed by memory in x
        # and self.gf is backed by memory in gx
        cur_pos = 0
        self.gf = []
        for i,p in enumerate(apt):
            p_size = prod(p.shape)
            # set up alias
            apt[i] = x[cur_pos:cur_pos+p_size].reshape(p.shape)
            self.gf.append(gx[cur_pos:cur_pos+p_size].reshape(p.shape))
            # initialize with p's value
            apt[i][:] = p
            cur_pos += p_size

        f_x = f(*[p.copy() for p in apt])

        # now iterate over the elements of x, and call f on apt.
        x_copy = x.copy()
        for i in xrange(total_size):
            x[:] = x_copy

            x[i] += eps
            f_eps = f(*apt)
            gx[i] = numpy.asarray((f_eps - f_x)/eps)

        if packed_pt:
            self.gf = self.gf[0]

    @staticmethod
    def abs_rel_err(a,b,eps=1.0e-10):
        """Return a small number when a and b are close, relative to how big they are"""
        return abs(a-b) / (abs(a)+abs(b)+eps)

    def max_err(self, g_pt):
        """Return the biggest relative error between g_pt and self.gf"""
        if len(g_pt) != len(self.gf):
            raise ValueError('argument has wrong number of elements', len(g_pt))
        errs = []
        for i, (a, b) in enumerate(zip(g_pt, self.gf)):
            if a.shape != b.shape:
                raise ValueError('argument element %i has wrong shape %s' %(i,str((a.shape,
                    b.shape))))
            errs.append(numpy.max(numeric_grad.abs_rel_err(a,b)))
        if numpy.all(numpy.isfinite(errs)):
            return numpy.max(errs), numpy.argmax(errs)
        else:
            return float('inf'), 0


def verify_grad(op, pt, n_tests=2, rng=None, eps=None, tol=None, mode=None, cast_to_output_type=False):
    """ WRITEME

    Raises an Exception if the difference between the analytic gradient and
    numerical gradient (computed through the Finite Difference Method) exceeds
    the given tolerance.

    :param op: something that behaves like an Op instance with a single output
               (can be a python function combining multiple ops, but see note below)
    :param pt: the list of numpy.ndarrays to use as inputs to the op
    :param n_tests: number of times to run the test
    :param rng: random number generator from which to draw random samples
    :param eps: stepsize used in the Finite Difference Method (Default None is type-dependent)
    :param tol: relative tolerance used as threshold for gradient comparison

    :note: WARNING to unit-test writers: if `op` is a function that builds a graph,
           try to make it a SMALL graph.  Often verify grad is run in
           debug mode, which can be very slow if it has to verify a lot
           of intermediate computations.

    """
    pt = [numpy.array(p) for p in pt]

    _type_tol = dict( # relativ error tolerances for different types
            float32=1e-2,
            float64=1e-4)

    if tol is None:
        tol = __builtin__.max(_type_tol[str(p.dtype)] for p in pt)

    if rng is None:
        raise TypeError('rng should be a valid instance of numpy.random.RandomState.',
                'You may want to use theano.tests.unittest_tools.verify_grad instead of theano.tensor.verify_grad.')

    def function(inputs, output):
        if mode is None:
            f = compile.function(inputs, output, accept_inplace=True)
        else:
            f = compile.function(inputs, output, accept_inplace=True, mode=mode)
        return f

    for test_num in xrange(n_tests):

        tensor_pt = [value(p.copy(), name='input %i'%i) for i,p in enumerate(pt)]

        #op can be either a function or an actual Op instance
        o_output = op(*tensor_pt)

        if isinstance(o_output,list) > 1:
            raise NotImplementedError('cant (yet) autotest gradient of op with multiple outputs')
            # we could make loop over outputs making random projections R for each,
            # but this doesn't handle the case where not all the outputs are
            # differentiable... so I leave this as TODO for now -JB.

        o_fn = function(tensor_pt, o_output)
        o_fn_out = o_fn(*[p.copy() for p in pt])
        if isinstance(o_fn_out, tuple) or isinstance(o_fn_out, list):
            raise TypeError('It seems like you are trying to use verify_grad '
                    'on an op or a function which outputs a list: there should'
                    ' be a single (array-like) output instead')

        # random_projection should not have elements too small,
        # otherwise too much precision is lost in numerical gradient
        random_projection = rng.rand(*o_fn_out.shape) + 0.5

        if cast_to_output_type:
            random_projection = numpy.array(random_projection,
                                            dtype=o_output.dtype)

        t_r = as_tensor_variable(random_projection)

        #random projection of o onto t_r
        cost = sum(t_r * o_output)  #This sum() is defined above, it's not the builtin sum.
        cost_fn = function(tensor_pt, cost)

        num_grad = numeric_grad(cost_fn, [p.copy() for p in pt], eps)

        g_cost = as_tensor_variable(1.0,name='g_cost')
        if cast_to_output_type:
            g_cost = cast(g_cost, o_output.dtype)

        symbolic_grad = grad(cost, tensor_pt, g_cost)

        grad_fn = function(tensor_pt, symbolic_grad)

        analytic_grad = grad_fn(*[p.copy() for p in pt])

        if not isinstance(analytic_grad, (list, tuple)):
            analytic_grad = [analytic_grad]

        max_err, max_err_pos = num_grad.max_err(analytic_grad)
        if  max_err > tol:
            raise Exception(verify_grad.E_grad, (max_err, tol, max_err_pos))

verify_grad.E_grad = 'gradient error exceeded tolerance'
"""This error is raised when a gradient is calculated, but incorrect."""
