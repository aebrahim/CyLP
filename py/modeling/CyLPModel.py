'''
    Module provide a facility to model a linear program.
    Although the current usage is to pass the resulting LP to CLP
    to solve it is independant of the solver.

    .. _modeling-usage:

    **Usage**

    >>> import numpy as np
    >>> from CyLP.cy import CyClpSimplex
    >>> from CyLP.py.modeling.CyLPModel import CyLPModel, CyLPArray
    >>>
    >>> model = CyLPModel()
    >>>
    >>> # Add variables
    >>> x = model.addVariable('x', 3)
    >>> y = model.addVariable('y', 2)
    >>>
    >>> # Create coefficients and bounds
    >>> A = np.matrix([[1., 2., 0],[1., 0, 1.]])
    >>> B = np.matrix([[1., 0, 0], [0, 0, 1.]])
    >>> D = np.matrix([[1., 2.],[0, 1]])
    >>> a = CyLPArray([5, 2.5])
    >>> b = CyLPArray([4.2, 3])
    >>> x_u= CyLPArray([2., 3.5])
    >>>
    >>> # Add constraints
    >>> model.addConstraint(A * x <= a)
    >>> model.addConstraint(2 <= B * x + D * y <= b)
    >>> model.addConstraint(y >= 0)
    >>> model.addConstraint(1.1 <= x[1:3] <= x_u)
    >>>
    >>> # Set the objective function
    >>> c = CyLPArray([1., -2., 3.])
    >>> model.objective = c * x + 2 * y
    >>>
    >>> # Create a CyClpSimplex instance from the model
    >>> s = CyClpSimplex(model)
    >>>
    >>> # Solve using primal Simplex
    >>> s.primal()
    'optimal'
    >>> s.primalVariableSolution
    array([ 0.2,  2. ,  1.1,  0. ,  0.9])
    >>> s.addConstraint(x[2] + y[1] >= 2.1)
    >>> s.primal()
    'optimal'
    >>> s.primalVariableSolution
    array([ 0. ,  2. ,  1.1,  0. ,  1. ])

'''


import hashlib
import numpy as np
from scipy import sparse

from CyLP.py.utils.sparseUtil import sparseConcat


class CyLPExpr:
    operators = ('>=', '<=', '==', '+', '-', '*', 'u-', 'sum')

    def __init__(self, opr='', left='', right=''):
        self.opr = opr
        self.left = left
        self.right = right
        self.expr = self
        #self.variables = []

    def __hash__(self):
        return id(self)

    def __repr__(self):
        s = '%s %s %s' % (self.left, self.right, self.opr)
        return s

    def __le__(self, other):
        v = CyLPExpr(opr="<=", right=other, left=self.expr)
        self.expr = v
        return v

    def __ge__(self, other):
        v = CyLPExpr(opr=">=", right=other, left=self.expr)
        self.expr = v
        return v

    def __eq__(self, other):
        # Check if both sides are CyLPVar, in which case a pythonic
        # comparison is meant (for dictionary keys,...)
        if isinstance(self, CyLPVar) and isinstance(other, CyLPVar):
            return (str(self) == str(other))
        v = CyLPExpr(opr="==", right=other, left=self.expr)
        self.expr = v
        return v

    def __rmul__(self, other):
        v = CyLPExpr(opr="*", left=other, right=self)
        v.expr = v
        self.expr = v
        return v

    def __rsub__(self, other):
        v = CyLPExpr(opr="-", left=other, right=self)
        v.expr = v
        self.expr = v
        return v

    def __radd__(self, other):
        v = CyLPExpr(opr="+", left=other, right=self)
        v.expr = v
        self.expr = v
        return v

    def __neg__(self):
        v = CyLPExpr(opr="u-", right=self)
        v.expr = v
        self.expr = v
        return v

    def getPostfix(self):
        if isinstance(self, CyLPVar):
            return [self]
        left = [self.left]
        right = [self.right]
        if isinstance(self.left, CyLPExpr):
            left = self.left.getPostfix()
        if isinstance(self.right, CyLPExpr):
            right = self.right.getPostfix()

        return left + right + [self.opr]

    def evaluate(self):
        '''Evaluates an expression in the postfix form
        '''
        tokens = self.getPostfix()
        operands = []

        cons = CyLPConstraint()

        for token in tokens:
            # If an operand found
            if type(token) != str or token not in CyLPExpr.operators:
                operands.append(token)
                if (isinstance(token, CyLPVar)):
                    varToBeAdded = token
                    if not varToBeAdded in cons.variables:
                        cons.variables.append(varToBeAdded)

            # Else we know what to do with an operator
            else:
                if token == 'u-':
                    right = operands.pop()
                    cons.perform(token, right=right)
                    operands.append(CyLPExpr(token, right=right))
                else:
                    right = operands.pop()
                    left = operands.pop()
                    cons.perform(token, left, right)
                    operands.append(CyLPExpr(token, left, right))

        # If something remains in the *operands* list, it means
        # that we have a single CyLPVar object on a line with no
        # operators operating on it. Check this situation and
        # add a 1 vector as its coefficient
        if len(operands) == 1 and isinstance(operands[0], CyLPVar):
            var = operands[0]
            cons.varCoefs[var] = np.ones(var.dim)

        # Reset variables for future constraints
        for var in cons.varCoefs.keys():
            var.expr = var

        return cons


class CyLPConstraint:
    def __init__(self):
        self.varNames = []
        self.parentVarDims = {}
        self.varCoefs = {}
        self.lower = None
        self.upper = None
        self.nRows = None
        self.isRange = True
        self.variables = []

    def __repr__(self):
        s = ''
        s += 'variable names:\n'
        s += str(self.varNames) + '\n'
        s += 'coefficients:\n'
        s += str(self.varCoefs) + '\n'
        s += 'lower = %s\n' % str(self.lower)
        s += 'upper = %s\n' % str(self.upper)
        if self.isRange:
            s += 'Constraint is a range\n'
        else:
            s += 'normal Constarint\n'
        return s

    def perform(self, opr, left=None, right=None):
        if isinstance(right, CyLPVar):
            if right.name not in self.varNames:
                self.varNames.append(right.name)
                self.parentVarDims[right.name] = right.parentDim
            if opr == 'u-':
                ones = -CyLPArray(np.ones(right.dim))
                self.varCoefs[right] = ones
                self.isRange = False
                self.nRows = right.dim
            elif opr == 'sum':
                pass
            else:
                #self.varNames.append(right.name)
                #self.parentVarDims[right.name] = right.parentDim
                dim = right.dim

                if opr == "*":  # Setting the coefficients
                    if isinstance(left, (float, long, int)):
                        if self.nRows and self.nRows != 1:
                            raise Exception("Expected 1-dimensional" \
                                            " coefficient")
                        self.nRows = 1
                        coef = CyLPArray(left * np.ones(dim))
                    else:
                        if right.dim != left.shape[-1]:
                            raise Exception("Coefficient:\n%s\n has %d" \
                                            " columns expected %d"
                                        % (left, left.shape[1], right.dim))
                        if len(left.shape) == 1:  # Array
                            nr = 1
                        else:
                            nr = len(left)
                        if self.nRows and self.nRows != nr:
                            raise Exception("Coefficient:\n%s\n has %d rows" \
                                            " expected %d"
                                        % (left, left.shape[0], self.nRows))
                        self.nRows = nr
                        coef = left
                    self.varCoefs[right] = coef
                    self.isRange = False

                if opr == '+' or opr == '-':
                    # No coefs for left op
                    self.isRange = False
                    if isinstance(left, CyLPVar):
                        ones = CyLPArray(np.ones(left.dim))
                        self.varCoefs[left] = ones
                        self.nRows = 1
                        if left.name not in self.varNames:
                            self.varNames.append(left.name)
                            self.parentVarDims[left.name] = left.parentDim
                    # No coefs for right op
                    if isinstance(right, CyLPVar):
                        if right in self.varCoefs.keys():
                            self.varCoefs[right] *= -1
                        else:
                            ones = CyLPArray(np.ones(right.dim))
                            if opr == '-':
                                ones *= -1
                            self.varCoefs[right] = ones
                            self.nRows = 1
                            if right.name not in self.varNames:
                                self.varNames.append(right.name)
                                self.parentVarDims[right.name] = \
                                                            right.parentDim

        # check if no coef for left
        if left.__class__ == CyLPVar and (opr == '-' or opr == '+'):
            self.isRange = False
            ones = CyLPArray(np.ones(left.dim))
            self.varCoefs[left] = ones
            self.nRows = 1
            if left.name not in self.varNames:
                self.varNames.append(left.name)
                self.parentVarDims[left.name] = left.parentDim

        # Coef already set for the right operand
        if right.__class__ == CyLPExpr and opr == '-':
            # The expression on the right is in the form (left opr right)
            # so the key to the coef in self.varCoefs is right.right i.e.
            # the CyLPVar on the right
            self.isRange = False
            self.varCoefs[right.right] *= -1

        if opr in ('<=', '>=', '=='):
            if isinstance(left, CyLPExpr) and not isinstance(right,
                                                              CyLPExpr):
                bound = right
            elif isinstance(right, CyLPExpr) and not isinstance(left,
                                                                CyLPExpr):
                bound = left
            else:
                raise Exception('At least one side of a comparison sign' \
                                'should be constant.')

            if self.isRange:
                var = self.variables[0]
                self.nRows = var.parentDim
                dim = var.parentDim
            #if not self.nRows:
                #self.nRows = left.parentDim
                #dim = left.parentDim
            else:
#                # FIXME: Think of the correct way to check bound dimensions
#                if (not isinstance(right, (float, long, int)) and
#                            right.shape[0] != self.nRows):
#                    #raise Exception('Bound dim: %d, expected dim: %d '
#                    #                % (right.shape[0], self.nRows))
#                    pass
                dim = self.nRows
#
            if isinstance(right, (float, long, int)):
                bound = CyLPArray(right * np.ones(dim))

            if self.isRange:
                if ((opr in ('>=', '==') and isinstance(left, CyLPExpr)) or
                    (opr in ('<=', '==') and isinstance(right, CyLPExpr))):
                    if var.parent:
                        var.parent.lower[var.indices] = bound
                    else:
                        var.lower[var.indices] = bound

                if ((opr in ('<=', '==') and isinstance(left, CyLPExpr)) or
                    (opr in ('>=', '==') and isinstance(right, CyLPExpr))):
                    if var.parent:
                        var.parent.upper[var.indices] = bound
                    else:
                        var.upper[var.indices] = bound

            else:
                if ((opr in ('>=', '==') and isinstance(left, CyLPExpr)) or
                    (opr in ('<=', '==') and isinstance(right, CyLPExpr))):
                    self.lower = bound
                    if self.upper == None:
                        self.upper = getCoinInfinity() * np.ones(len(bound))
                if ((opr in ('<=', '==') and isinstance(left, CyLPExpr)) or
                    (opr in ('>=', '==') and isinstance(right, CyLPExpr))):
                    self.upper = bound
                    if self.lower == None:
                        self.lower = -getCoinInfinity() * np.ones(len(bound))


class CyLPVar(CyLPExpr):
    '''
    Contains variable information such as its name, dimension,
    bounds, and whether or not it is an integer. We don't creat
    instances directly but rather use :py:func:`CyLPModel.addVariable`
    to create one.

    See the :ref:`modeling example <modeling-usage>`.

    '''

    def __init__(self, name, dim, isInt=False, fromInd=None, toInd=None):
        self.name = name
        self.dim = dim
        self.parentDim = dim
        self.parent = None
        self.isInt = isInt
        self.expr = self
        self.lower = -getCoinInfinity() * np.ones(dim)
        self.upper = getCoinInfinity() * np.ones(dim)

        self.indices = self.fromInd = self.toInd = None
        if fromInd and toInd:
            self.indices = range(fromInd, toInd)
            self.formInd = fromInd
            self.toInd = toInd

    def __repr__(self):
        s = self.name
        if self.fromInd and self.toInd:
            s += '[%d:%d]' % (self.fromInd, self.toInd)
        elif self.indices != None and len(self.indices) == 1:
            s += '[%d]' % self.indices
        return s

    def __getitem__(self, key):
        if type(key) == int:
            newObj = CyLPVar(self.name, 1)
            newObj.indices = np.array([key], np.int32)
            newObj.parentDim = self.dim
            newObj.parent = self
            newObj.dim = 1
        elif type(key) == slice:
            newObj_fromInd = key.start
            if key.stop > self.dim:
                newObj_toInd = self.dim
            else:
                newObj_toInd = key.stop

            newObj = CyLPVar(self.name, newObj_toInd - newObj_fromInd)
            newObj.parentDim = self.dim
            newObj.parent = self
            newObj.fromInd = newObj_fromInd
            newObj.toInd = newObj_toInd
            newObj.indices = np.arange(newObj.fromInd, newObj.toInd)
            # Save parentDim for future use
            newObj.dim = len(newObj.indices)

        return newObj

    def sum(self):
        v = CyLPExpr(opr="sum", right=self)
        v.expr = v
        self.expr = v
        return v


class CyLPArray(np.ndarray):
    '''
    It is a tweaked Numpy array. It allows user to define objects
    comparability to an array. If we have an instance of ``CyLPVar``
    called ``x`` and a numpy array ``b``, then ``b >= x`` returns an array
    of the same dimension as ``b`` with all elements equal to ``False``,
    which has no sense at all. On the constrary ``CyLPArray`` will return
    ``NotImplemented`` if it doesn't know how to compare. This is particularly
    helpful when we define LP constraints.

    See the :ref:`modeling example <modeling-usage>`.

    '''

    def __new__(cls, input_array, info=None):
        # Input array is an already formed ndarray instance
        # We first cast to be our class type
        obj = np.asarray(input_array).view(cls)
        # add the new attribute to the created instance
        obj.__le__ = CyLPArray.__le__
        obj.__ge__ = CyLPArray.__ge__
        return obj

    def __le__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__le__(self, other)

    def __ge__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__ge__(self, other)

    def __mul__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__mul__(self, other)

    def __rmul__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__rmul__(self, other)

    def __add__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__add__(self, other)

    def __radd__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__radd__(self, other)

    def __rsub__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__rsub__(self, other)

    def __sub__(self, other):
        if (issubclass(other.__class__, CyLPExpr)):
            return NotImplemented
        return np.ndarray.__sub__(self, other)


class IndexFactory:
    '''
    A small class that keeps track of the indices of variables and constraints
    that you are adding to a problem gradually.

    **Usage**

    >>> from CyLP.py.modeling.CyLPModel import IndexFactory
    >>> i = IndexFactory()
    >>> i.addVar('x', 10)
    >>> i.addVar('y', 5)
    >>> i.hasVar('x')
    True
    >>> i.varIndex['y'] == range(10, 15)
    True
    >>> i.addConst('Manpower', 4)
    >>> i.addConst('Gold', 10)
    >>> i.constIndex['Manpower'] == range(4)
    True
    >>> i.constIndex['Gold'] == range(4, 14)
    True

    '''

    def __init__(self):
        self.varIndex = {}
        self.constIndex = {}
        self.currentVarIndex = 0
        self.currentConstIndex = 0

    def addVar(self, varName, numberOfVars):
        if numberOfVars == 0:
            return
        if varName in self.varIndex.keys():
            self.varIndex[varName] += range(self.currentVarIndex,
                                            self.currentVarIndex +
                                            numberOfVars)
        else:
            self.varIndex[varName] = range(self.currentVarIndex,
                                            self.currentVarIndex +
                                            numberOfVars)
        self.currentVarIndex += numberOfVars

    def hasVar(self, varName):
        return varName in self.varIndex.keys()

    def hasConst(self, constName):
        return constName in self.constIndex.keys()

    def getLastVarIndex(self):
        return self.currentVarIndex - 1

    def addConst(self, constName, numberOfConsts):
        if numberOfConsts == 0:
            return
        if constName in self.constIndex.keys():
            self.constIndex[constName] += range(self.currentConstIndex,
                    self.currentConstIndex + numberOfConsts)
        else:
            self.constIndex[constName] = range(self.currentConstIndex,
                                            self.currentConstIndex +
                                            numberOfConsts)
        self.currentConstIndex += numberOfConsts

    def getLastConstIndex(self):
        return self.currentConstIndex - 1

    def __repr__(self):
        varind = self.varIndex
        cind = self.constIndex

        s = "variables : \n"
        for vname, rg in varind.items():
            s = '%s : %s\n' % (vname.rjust(15), str(rg))
        s += '\n'
        s += "constraints : \n"
        for cname, rg in cind.items():
            s += '%s : %s\n' % (cname.rjust(15), str(rg))
        return s


class CyLPModel(object):
    '''
    Hold all the necessary information to create a linear program, i.e.
    variables, constraints, and the objective function.

    See the :ref:`modeling example <modeling-usage>`.

    '''

    def __init__(self):
        self.variables = []
        self.constraints = []
        self.objective_ = None
        self.inds = IndexFactory()

    def addVariable(self, name, dim, isInt=False):
        '''
        Create a new instance of :py:class:`CyLPVar` using the given
        arguments and add it to current model's variable list.
        '''
        var = CyLPVar(name, dim, isInt)
        self.variables.append(var)
        return var

    def makeIndexFactory(self):
        self.allVarNames = []
        self.allParentVarDims = {}
        self.nRows = 0
        for c in self.constraints:
            # Make sure to add variables in the same order they were
            # added to CyLPModel
            for var in self.variables:
                self.allVarNames += [vname for vname in c.varNames
                                    if vname not in self.allVarNames and
                                    vname == var.name]
            self.allParentVarDims.update(c.parentVarDims)
            self.nRows += c.nRows

        for varName in self.allVarNames:
            if not self.inds.hasVar(varName):
                self.inds.addVar(varName, self.allParentVarDims[varName])

    #def getConstraintVars(self, cons):
    #    return cons.variables

    @property
    def objective(self):
        return self.objective_

    @objective.setter
    def objective(self, obj):
        self.objective_ = obj.evaluate()
        obj = np.array([])
        for varName in self.allVarNames:
            v_coef = self.generateVarObjCoef(varName)
            obj = np.concatenate((obj, v_coef), axis=0)
        self.objective_ = obj

    def addConstraint(self, cons):
        '''
        Add constraint ``cons`` to the ``CyLPModel``. Argument ``cons`` must
        be an expresion made using instances of :py:class:`CyLPVar`,
        :py:class:`CyLPArray`, Numpy matrices, or sparse matrices. It can
        use normal operators such as ``>=``, ``<=``, ``==``, ``+``, ``-``,
        ``*``.

        See the :ref:`modeling example <modeling-usage>`.

        '''

        c = cons.evaluate()
        if not c.isRange:
            self.constraints.append(cons.evaluate())
        self.makeMatrices()

    def generateVarObjCoef(self, varName):
        dim = self.allParentVarDims[varName]
        coef = np.zeros(dim)
        obj = self.objective_
        keys = [k for k in obj.varCoefs.keys() if k.name == varName]

        for var in keys:
            coef[var.indices] = obj.varCoefs[var]
        return coef

    def generateVarMatrix(self, varName):
        '''
        Create the coefficient matrix for a variable named
        varName. Returns a csr_matrixPlus
        '''
        dim = self.allParentVarDims[varName]
        mainCoef = None
        for c in self.constraints:
            keys = [k for k in c.varCoefs.keys() if k.name == varName]
            if not keys:
                coef = sparse.coo_matrix((c.nRows, dim))
            elif c.nRows == 1:
                coef = np.zeros(dim)
                for var in keys:
                    coef[var.indices] += c.varCoefs[var]

            else:  # Constraint has matrix coefficients
                # Initial coef with the coef of the first occurance
                coef = c.varCoefs[keys[0]]
                for var in keys[1:]:
                    coef = sparseConcat(coef, c.varCoefs[var], 'v')

            mainCoef = sparseConcat(mainCoef, coef, 'v')

        return mainCoef

    def makeMatrices(self):
        '''
        Makes coef matrix and rhs vector from CyLPConastraints
        in self.constraints
        '''
        #if len(self.constraints) == 0:
        #    return

        self.makeIndexFactory()
        #self.getVarBounds()

        # Create the aggregated coef matrix
        masterCoefMat = None
        for varName in self.allVarNames:
            vmat = self.generateVarMatrix(varName)
            masterCoefMat = sparseConcat(masterCoefMat, vmat, 'h')

        # Create bound vectors
        c_lower = np.array([])
        c_upper = np.array([])
        for c in self.constraints:
            if not c.isRange:
                c_lower = np.concatenate((c_lower, c.lower), axis=0)
                c_upper = np.concatenate((c_upper, c.upper), axis=0)

        # Create variables bound vectors
        v_lower = np.array([])
        v_upper = np.array([])
        for v in self.variables:
            v_lower = np.concatenate((v_lower, v.lower), axis=0)
            v_upper = np.concatenate((v_upper, v.upper), axis=0)

        return masterCoefMat, c_lower, c_upper, v_lower, v_upper


def getCoinInfinity():
    return 1.79769313486e+308


#model = CyLPModel()
#
#x = model.addVariable('x', 5)
#y = model.addVariable('y', 4)
#z = model.addVariable('z', 5)
#
#
#b = CyLPArray([3.1, 4.2])
#aa = np.matrix([[1, 2, 3, 3, 4], [3, 2, 1, 2, 5]])
#aa = np.matrix([[0, 0, 0, -1.5, -1.5], [-3, 0, 0, -2,0 ]])
#dd = np.matrix([[1, 3, 1, 4], [2, 1,3, 5]])
#
#
#model.addConstraint(1.5 <=  x[1:3] + 5 * y[3:] - 6 * x[2:5]  + x[0] <= 14)
#
#print model.constraints[0].varCoefs
#
#model.addConstraint(2 <= aa*x + dd * y <= b)
#model.addConstraint(x[2:4] >= 3)
#model.addConstraint(1.1 <= y <= CyLPArray([1,2,3,4]))
#
#
#A = model.makeMatrices()
#
#print 'x:'
#print x.lower
#print x.upper
#print 'y:'
#print y.lower
#print y.upper
#
#print '+++++++++++++++++++++++++++++'
##cc = x[0] - 2*x[1:3] -2.5 * y[1]
##cc = cc.evaluate()
#model.objective = x[0] - 2*x[1:3] -2.5 * y[1]
##print model.allParentVarDims
##print model.generateVarObjCoef('x')
##print model.generateVarObjCoef('y')
#
#print model.objective
##print cl
##print cu