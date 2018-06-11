# k_insertion.py
# An implementation of the k-insertion attack where the attacker adds k data
# points to the model
# Matthew Sedam

from adlib.adversaries.adversary import Adversary
from adlib.adversaries.datamodification.data_modification import \
    DataModification
from data_reader.binary_input import Instance
from data_reader.binary_input import BinaryFeatureVector
import math
import multiprocessing as mp
import numpy as np
from copy import deepcopy
from typing import List, Dict


class KInsertion(Adversary):
    """
    Performs a k-insertion attack where the attacked data is the original data
    plus k feature vectors designed to induce the most error in poison_instance.
    """

    def __init__(self, learner, poison_instance, alpha=1e-3, beta=0.05,
                 max_iter=2000, number_to_add=10, verbose=False):

        """
        :param learner: the trained learner
        :param poison_instance: the instance in which to induce the most error
        :param alpha: convergence condition (diff <= alpha)
        :param beta: the learning rate
        :param max_iter: the maximum number of iterations
        :param number_to_add: the number of new instances to add
        :param verbose: if True, print the feature vector and gradient for each
                        iteration
        """

        Adversary.__init__(self)
        self.learner = deepcopy(learner)
        self.poison_instance = poison_instance
        self.alpha = alpha
        self.beta = beta
        self.max_iter = max_iter
        self.orig_beta = beta
        self.number_to_add = number_to_add
        self.verbose = verbose
        self.instances = None
        self.orig_instances = None
        self.fvs = None  # feature vectors
        self.labels = None  # labels
        self.x = None  # The feature vector of the instance to be added
        self.y = None  # x's label
        self.inst = None
        self.kernel = self._get_kernel()
        self.kernel_derivative = self._get_kernel_derivative()
        self.z_c = None
        self.matrix = None
        self.quick_calc = None
        self.poison_loss_before = None
        self.poison_loss_after = None

    def attack(self, instances) -> List[Instance]:
        """
        Performs a k-insertion attack
        :param instances: the input instances
        :return: the attacked instances
        """

        if len(instances) == 0:
            raise ValueError('Need at least one instance.')

        self.orig_instances = deepcopy(instances)
        self.instances = self.orig_instances
        self.beta /= instances[0].get_feature_count()  # scale beta
        self.learner.training_instances = self.instances
        learner = self.learner.model.learner
        learner.fit(self.fvs, self.labels)

        self.poison_loss_before = self._calc_inst_loss(self.poison_instance)

        for k in range(self.number_to_add):
            # x is the full feature vector of the instance to be added
            self.x = np.random.binomial(1, 0.5,
                                        instances[0].get_feature_count())
            self.y = -1 if np.random.binomial(1, 0.5, 1)[0] == 0 else 1

            self._generate_inst()
            self.beta = self.orig_beta

            # Main learning loop for one insertion
            grad_norm = 0.0
            iteration = 0
            while (iteration == 0 or (grad_norm > self.alpha and
                                      iteration < self.max_iter)):

                print('Iteration: ', iteration, ' - gradient norm: ', grad_norm,
                      sep='')

                # Train with newly generated instance
                self.instances.append(self.inst)
                self.learner.training_instances = self.instances

                self.fvs = self.fvs.tolist()
                self.fvs.append(self.x)
                self.fvs = np.array(self.fvs)

                self.labels = self.labels.tolist()
                self.labels.append(self.y)
                self.labels = np.array(self.labels)

                learner.fit(self.fvs, self.labels)

                # Update feature vector of the instance to be added
                gradient = self._calc_gradient()
                grad_norm = np.linalg.norm(gradient)

                self.x = self.x - self.beta * gradient
                self.x = DataModification.project_feature_vector(self.x)
                self._generate_inst()
                self.instances = self.instances[:-1]
                self.fvs = self.fvs[:-1]
                self.labels = self.labels[:-1]

                if self.verbose:
                    print('Current feature vector:\n', self.x)

                iteration += 1

            print('Iteration: FINAL - gradient norm: ', grad_norm, sep='')
            print('Number added so far: ', k + 1, sep='')

            # Add the newly generated instance and retrain with that dataset
            self.instances.append(self.inst)
            self.learner.training_instances = self.instances
            self.learner.train()

        self.poison_loss_after = self._calc_inst_loss(self.poison_instance)

        return self.instances

    def _calculate_constants(self):
        """
        Calculates constants for the gradient descent loop
        """

        # Calculate feature vectors
        self.fvs = []
        for i in range(len(self.instances)):
            feature_vector = self.instances[i].get_feature_vector()
            tmp = []
            for j in range(self.instances[0].get_feature_count()):
                if feature_vector.get_feature(j) == 1:
                    tmp.append(1)
                else:
                    tmp.append(0)
            tmp = np.array(tmp)
            self.fvs.append(tmp)
        self.fvs = np.array(self.fvs, dtype='float64')

        # Calculate labels
        self.labels = []
        for inst in self.instances:
            self.labels.append(inst.get_label())
        self.labels = np.array(self.labels)

    def _calc_inst_loss(self, inst: Instance):
        """
        Calculates the logistic loss for one instance
        :param inst: the instance
        :return: the logistic loss
        """

        fv = []
        for i in range(inst.get_feature_count()):
            if inst.get_feature_vector().get_feature(i) == 1:
                fv.append(1)
            else:
                fv.append(0)
        fv = np.array(fv)

        # reshape is for the decision function when inputting only one sample
        loss = self.learner.model.learner.decision_function(fv.reshape(1, -1))
        loss *= -1 * inst.get_label()
        loss = math.log(1 + math.exp(loss))

        return loss

    def _generate_inst(self):
        """
        :return: a properly generated Instance that has feature vector self.x
                 and label self.y
        """

        indices_list = []
        for i in range(len(self.x)):
            if self.x[i] >= 0.5:
                indices_list.append(i)

        # Generate new instance
        self.inst = Instance(self.y,
                             BinaryFeatureVector(len(self.x), indices_list))

    def _calc_gradient(self):
        """
        :return: the calculated gradient, an np.ndarray
        """

        result = self._solve_matrix()
        self.z_c = result[0]
        self.matrix = result[1]

        # If resulting matrix is zero (it will be if z_c == 0 by definition, so
        # short-circuit behavior is being used here), then only do one
        # calculation as per the formula.
        if self.z_c == 0 or np.count_nonzero(self.matrix) == 0:
            self.quick_calc = True
        else:
            self.quick_calc = False

        size = self.instances[0].get_feature_count()
        pool = mp.Pool(mp.cpu_count())
        gradient = list(pool.map(self._calc_grad_helper, range(size)))
        pool.close()
        pool.join()

        gradient = np.array(gradient)

        if self.verbose:
            print('\nCurrent gradient:\n', gradient)

        return gradient

    def _calc_grad_helper(self, i):
        """
        Helper function for gradient. Calculates one partial derivative.
        :param i: determines which partial derivative
        :return: the partial derivative
        """

        if self.quick_calc:
            val = self._Q(self.instances[-1], self.inst, True, i) * self.z_c
            return val
        else:
            current = 0  # current partial derivative

            vector = [0]
            for j in self.learner.model.learner.support_:
                vector.append(
                    self._Q(self.instances[j], self.inst, True, i))
            vector = np.array(vector)

            solution = self.matrix.dot(vector)
            partial_b_partial_x_k = solution[0]
            partial_z_s_partial_x_k = solution[1:]

            s_v_indices = self.learner.model.learner.support_.tolist()
            for j in range(len(self.orig_instances)):
                if j in self.learner.model.learner.support_:
                    q_i_t = self._Q(self.orig_instances[j], self.inst)
                    partial_z_i_partial_x_k = partial_z_s_partial_x_k[
                        s_v_indices.index(j)]
                    current += q_i_t * partial_z_i_partial_x_k

            current += (self._Q(self.instances[-1], self.inst, True, i) *
                        self.z_c)

            if len(self.instances) in self.learner.model.learner.support_:
                current += (self._Q(self.instances[-1], self.inst) *
                            partial_z_s_partial_x_k[-1])

            current += self.inst.get_label() * partial_b_partial_x_k
            return current

    def _solve_matrix(self):
        """
        :return: z_c, matrix for derivative calculations

        Note: I tried using multiprocessing Pools, but these were slower than
              using the built-in map function.
        """

        learner = self.learner.model.learner
        size = learner.n_support_[0] + learner.n_support_[1] + 1  # binary
        matrix = np.full((size, size), 0)

        if len(self.instances) - 1 not in learner.support_:  # not in S
            if self.learner.predict(self.inst) != self.inst.get_label():  # in E
                z_c = learner.C
            else:  # in R, z_c = 0, everything is 0
                return 0, matrix
        else:  # in S
            # Get index of coefficient
            index = learner.support_.tolist().index(len(self.instances) - 1)
            z_c = learner.dual_coef_.flatten()[index]

        y_s = []
        for i in learner.support_:
            y_s.append(self.instances[i].get_label())
        y_s = np.array(y_s)

        q_s = []
        for i in range(size - 1):
            values = list(map(
                lambda idx: self._Q(self.instances[learner.support_[i]],
                                    self.instances[learner.support_[idx]]),
                range(size - 1)))
            q_s.append(values)
        q_s = np.array(q_s)

        for i in range(1, size):
            matrix[0][i] = y_s[i - 1]
            matrix[i][0] = y_s[i - 1]

        for i in range(1, size):
            for j in range(1, size):
                matrix[i][j] = q_s[i - 1][j - 1]

        try:
            matrix = np.linalg.inv(matrix)
        except np.linalg.linalg.LinAlgError:
            # Sometimes the matrix is reported to be singular. In this case,
            # the safest thing to do is have the matrix and thus eventually
            # the gradient equal 0 as to not move the solution incorrectly.
            # There is probably an error in the computation, but I have not
            # looked for it yet.
            print('SINGULAR MATRIX ERROR - FIX ME')
            z_c = 0

        matrix = -1 * z_c * matrix

        return z_c, matrix

    def _Q(self, inst_1: Instance, inst_2: Instance, derivative=False, k=-1):
        """
        Calculates Q_ij or partial Q_ij / partial x_k
        :param inst_1: the first instance
        :param inst_2: the second instance
        :param derivative: True -> calculate derivative, False -> calculate Q
        :param k: determines which derivative to calculate
        :return: Q_ij or the derivative where i corresponds to inst_1 and j
                 corresponds to inst_2
        """

        if inst_1.get_feature_count() != inst_2.get_feature_count():
            raise ValueError('Feature vectors need to have same length.')

        fv = [[], []]
        for i in range(2):
            if i == 0:
                inst = inst_1
            else:
                inst = inst_2

            feature_vector = inst.get_feature_vector()
            for j in range(inst.get_feature_count()):
                if feature_vector.get_feature(j) == 0:
                    fv[i].append(0)
                else:
                    fv[i].append(1)

        if derivative:
            ret_val = self.kernel_derivative(np.array(fv[0]),
                                             np.array(fv[1]),
                                             k)
        else:
            ret_val = self.kernel(np.array(fv[0]), np.array(fv[1]))
        return inst_1.get_label() * inst_2.get_label() * ret_val

    def _kernel_linear(self, fv_1: np.ndarray, fv_2: np.ndarray):
        """
        Returns the value of the specified kernel function
        :param fv_1: feature vector 1 (np.ndarray)
        :param fv_2: feature vector 2 (np.ndarray)
        :return: the value of the specified kernel function
        """

        if len(fv_1) != len(fv_2):
            raise ValueError('Feature vectors need to have same length.')

        return fv_1.dot(fv_2)

    def _kernel_derivative_linear(self, fv_1: np.ndarray,
                                  fv_2: np.ndarray, k: int):
        """
        Returns the value of the derivative of the specified kernel function
        with fv_2 being the variable (i.e. K(x_i, x_c), finding gradient
        evaluated at x_c
        :param fv_1: fv_1: feature vector 1 (np.ndarray)
        :param fv_2: fv_2: feature vector 2 (np.ndarray)
        :param k: which partial derivative (0-based indexing, int)
        :return: the value of the derivative of the specified kernel function
        """

        if len(fv_1) != len(fv_2) or k < 0 or k >= len(fv_1):
            raise ValueError('Feature vectors need to have same '
                             'length and k must be a valid index.')

        return fv_1[k]

    def _kernel_poly(self, fv_1: np.ndarray, fv_2: np.ndarray):
        """
        Returns the value of the specified kernel function
        :param fv_1: feature vector 1 (np.ndarray)
        :param fv_2: feature vector 2 (np.ndarray)
        :return: the value of the specified kernel function
        """

        if len(fv_1) != len(fv_2):
            raise ValueError('Feature vectors need to have same length.')

        return ((self.learner.gamma * fv_1.dot(fv_2) + self.learner.coef0) **
                self.learner.degree)

    def _kernel_derivative_poly(self, fv_1: np.ndarray,
                                fv_2: np.ndarray, k: int):
        """
        Returns the value of the derivative of the specified kernel function
        with fv_2 being the variable (i.e. K(x_i, x_c), finding gradient
        evaluated at x_c
        :param fv_1: fv_1: feature vector 1 (np.ndarray)
        :param fv_2: fv_2: feature vector 2 (np.ndarray)
        :param k: which partial derivative (0-based indexing, int)
        :return: the value of the derivative of the specified kernel function
        """

        if len(fv_1) != len(fv_2) or k < 0 or k >= len(fv_1):
            raise ValueError('Feature vectors need to have same '
                             'length and k must be a valid index.')

        return (fv_1[k] * self.learner.degree *
                self.learner.gamma *
                ((self.learner.gamma * fv_1.dot(fv_2) + self.learner.coef0) **
                 (self.learner.degree - 1)))

    def _kernel_rbf(self, fv_1: np.ndarray, fv_2: np.ndarray):
        """
        Returns the value of the specified kernel function
        :param fv_1: feature vector 1 (np.ndarray)
        :param fv_2: feature vector 2 (np.ndarray)
        :return: the value of the specified kernel function
        """

        if len(fv_1) != len(fv_2):
            raise ValueError('Feature vectors need to have same length.')

        norm = np.linalg.norm(fv_1 - fv_2) ** 2
        return math.exp(-1 * self.learner.gamma * norm)

    def _kernel_derivative_rbf(self, fv_1: np.ndarray,
                               fv_2: np.ndarray, k: int):
        """
        Returns the value of the derivative of the specified kernel function
        with fv_2 being the variable (i.e. K(x_i, x_c), finding gradient
        evaluated at x_c
        :param fv_1: fv_1: feature vector 1 (np.ndarray)
        :param fv_2: fv_2: feature vector 2 (np.ndarray)
        :param k: which partial derivative (0-based indexing, int)
        :return: the value of the derivative of the specified kernel function
        """

        if len(fv_1) != len(fv_2) or k < 0 or k >= len(fv_1):
            raise ValueError('Feature vectors need to have same '
                             'length and k must be a valid index.')

        return (self._kernel_rbf(fv_1, fv_2) * 2 *
                self.learner.gamma * (fv_1[k] - fv_2[k]))

    def _kernel_sigmoid(self, fv_1: np.ndarray, fv_2: np.ndarray):
        """
        Returns the value of the specified kernel function
        :param fv_1: feature vector 1 (np.ndarray)
        :param fv_2: feature vector 2 (np.ndarray)
        :return: the value of the specified kernel function
        """

        if len(fv_1) != len(fv_2):
            raise ValueError('Feature vectors need to have same length.')

        inside = self.learner.gamma * fv_1.dot(fv_2) + self.learner.coef0
        return math.tanh(inside)

    def _kernel_derivative_sigmoid(self, fv_1: np.ndarray,
                                   fv_2: np.ndarray, k: int):
        """
        Returns the value of the derivative of the specified kernel function
        with fv_2 being the variable (i.e. K(x_i, x_c), finding gradient
        evaluated at x_c
        :param fv_1: fv_1: feature vector 1 (np.ndarray)
        :param fv_2: fv_2: feature vector 2 (np.ndarray)
        :param k: which partial derivative (0-based indexing, int)
        :return: the value of the derivative of the specified kernel function
        """

        if len(fv_1) != len(fv_2) or k < 0 or k >= len(fv_1):
            raise ValueError('Feature vectors need to have same '
                             'length and k must be a valid index.')

        inside = self.learner.gamma * fv_1.dot(fv_2) + self.learner.coef0
        return self.learner.gamma * fv_1[k] / (math.cosh(inside) ** 2)

    def _get_kernel(self):
        """
        :return: the appropriate kernel function
        """

        if self.learner.model.learner.kernel == 'linear':
            return self._kernel_linear
        elif self.learner.model.learner.kernel == 'poly':
            return self._kernel_poly
        elif self.learner.model.learner.kernel == 'rbf':
            return self._kernel_rbf
        elif self.learner.model.learner.kernel == 'sigmoid':
            return self._kernel_sigmoid
        else:
            raise ValueError('No matching kernel function found.')

    def _get_kernel_derivative(self):
        """
        :return: the appropriate kernel derivative function
        """

        if self.learner.model.learner.kernel == 'linear':
            return self._kernel_derivative_linear
        elif self.learner.model.learner.kernel == 'poly':
            return self._kernel_derivative_poly
        elif self.learner.model.learner.kernel == 'rbf':
            return self._kernel_derivative_rbf
        elif self.learner.model.learner.kernel == 'sigmoid':
            return self._kernel_derivative_sigmoid
        else:
            raise ValueError('No matching kernel function found.')

    def set_params(self, params: Dict):
        if params['learner'] is not None:
            self.learner = params['learner']
        if params['poison_instance'] is not None:
            self.poison_instance = params['poison_instance']
        if params['alpha'] is not None:
            self.alpha = params['alpha']
        if params['beta'] is not None:
            self.beta = params['beta']
        if params['max_iter'] is not None:
            self.max_iter = params['max_iter']
        if params['number_to_add'] is not None:
            self.number_to_add = params['number_to_add']
        if params['verbose'] is not None:
            self.verbose = params['verbose']

        self.instances = None
        self.orig_instances = None
        self.fvs = None
        self.labels = None
        self.x = None
        self.y = None
        self.inst = None
        self.kernel = self._get_kernel()
        self.kernel_derivative = self._get_kernel_derivative()
        self.z_c = None
        self.matrix = None
        self.quick_calc = None
        self.poison_loss_before = None
        self.poison_loss_after = None

    def get_available_params(self):
        params = {'learner': self.learner,
                  'poison_instance': self.poison_instance,
                  'alpha': self.alpha,
                  'beta': self.beta,
                  'max_iter': self.max_iter,
                  'number_to_add': self.number_to_add,
                  'verbose': self.verbose}
        return params

    def set_adversarial_params(self, learner, train_instances):
        self.learner = learner
        self.instances = train_instances
